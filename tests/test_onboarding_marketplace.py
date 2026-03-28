from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel, Field, model_validator

from bub.onboarding.catalog import builtin_marketplace_manifests
from bub.onboarding.models import (
    OnboardingCondition,
    OnboardingField,
    OnboardingManifest,
    OnboardingOption,
    OnboardingStep,
    SecretRequirement,
)
from bub.onboarding.renderer import (
    ReviewSelection,
    _default_option_value,
    _field_guidance_text,
    _move_reorder_value,
    _prioritize_options,
    _render_typer_option_lines,
    _step_guidance_text,
    renderer_for_surface,
)
from bub.onboarding.service import MarketplaceService


def _service(tmp_path: Path) -> MarketplaceService:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    return MarketplaceService(workspace=workspace, home=home, manifests=builtin_marketplace_manifests())


class StubRenderer:
    def __init__(
        self,
        *,
        choice_values: list[str] | None = None,
        multi_choice_values: list[list[str]] | None = None,
        reorder_values: list[list[str]] | None = None,
        field_values: list[object] | None = None,
        secret_values: list[str] | None = None,
        confirm_values: list[bool] | None = None,
    ) -> None:
        self.choice_values = list(choice_values or [])
        self.multi_choice_values = list(multi_choice_values or [])
        self.reorder_values = list(reorder_values or [])
        self.field_values = list(field_values or [])
        self.secret_values = list(secret_values or [])
        self.confirm_values = list(confirm_values or [])
        self.review_values: list[ReviewSelection] = [ReviewSelection(action="install")]
        self.review_calls: list[tuple[str, str]] = []
        self.error_calls: list[tuple[str, str]] = []

    def render_info(self, *, manifest: OnboardingManifest, step: OnboardingStep) -> None:
        del manifest, step

    def render_external_link(self, *, manifest: OnboardingManifest, step: OnboardingStep) -> None:
        del manifest, step

    def render_qr_challenge(self, *, manifest: OnboardingManifest, step: OnboardingStep) -> None:
        del manifest, step

    def render_error(self, *, title: str, text: str) -> None:
        self.error_calls.append((title, text))

    def confirm(self, *, title: str, text: str, default: bool = False) -> bool:
        del title, text, default
        return self.confirm_values.pop(0)

    def choose_one(self, *, step: OnboardingStep, current=None, options=None) -> str:
        del step, current, options
        return self.choice_values.pop(0)

    def choose_many(self, *, step: OnboardingStep, current=None, options=None) -> list[str]:
        del step, current, options
        return self.multi_choice_values.pop(0)

    def reorder(self, *, step: OnboardingStep, current: list[str], options) -> list[str]:
        del step, current, options
        return self.reorder_values.pop(0)

    def prompt_field(self, *, field: OnboardingField, default=None):
        del field, default
        return self.field_values.pop(0)

    def prompt_secret(self, *, field: OnboardingField) -> str:
        del field
        return self.secret_values.pop(0)

    def review_summary(self, *, title: str, text: str, editable_steps) -> ReviewSelection:
        del editable_steps
        self.review_calls.append((title, text))
        return self.review_values.pop(0)


def test_marketplace_service_installs_and_validates_telegram(tmp_path: Path) -> None:
    service = _service(tmp_path)

    state = service.install(
        "telegram",
        config_updates={"allow_users": ["alice"], "allow_chats": ["-1001"], "proxy": "http://127.0.0.1:7890"},
        secret_values={"bot_token": "123:abc"},
    )
    report = service.validate("telegram")
    runtime = service.load_runtime("telegram")

    assert state.enabled is True
    assert report.ok is True
    assert runtime == {
        "token": "123:abc",
        "allow_users": "alice",
        "allow_chats": "-1001",
        "proxy": "http://127.0.0.1:7890",
    }
    assert service.enabled_channels() == ["telegram"]


def test_marketplace_service_materializes_agent_runtime_and_legacy_env(tmp_path: Path) -> None:
    service = _service(tmp_path)

    service.install(
        "agent",
        config_updates={"model": "openai:gpt-5", "openai_api_base": "https://api.openai.com/v1"},
        secret_values={"openai_api_key": "sk-test"},
    )
    report = service.validate("agent")
    runtime = service.load_runtime("agent")
    env = service.legacy_env("agent")

    assert report.ok is True
    assert runtime["model"] == "openai:gpt-5"
    assert runtime["api_key"]["openai"] == "sk-test"
    assert runtime["api_base"]["openai"] == "https://api.openai.com/v1"
    assert env["BUB_MODEL"] == "openai:gpt-5"
    assert env["BUB_OPENAI_API_KEY"] == "sk-test"
    assert env["BUB_OPENAI_API_BASE"] == "https://api.openai.com/v1"


def test_marketplace_service_install_with_reset_replaces_existing_config_and_secrets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="reset_demo",
        title="Reset Demo",
        summary="reset demo",
        steps=(
            OnboardingStep(
                id="config",
                kind="form",
                title="Config",
                fields=(OnboardingField(key="model", title="Model"),),
            ),
            OnboardingStep(
                id="secret",
                kind="secret_input",
                title="Secret",
                fields=(OnboardingField(key="api_key", title="API key", required=False),),
            ),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])

    service.install("reset_demo", config_updates={"model": "old-model"}, secret_values={"api_key": "old-secret"})
    state = service.install("reset_demo", config_updates={"model": "new-model"}, reset=True)

    assert state.config == {"model": "new-model"}
    assert state.secret_refs == {}
    assert service.resolve_secret("reset_demo", "api_key") is None


def test_marketplace_service_tracks_install_sessions(tmp_path: Path) -> None:
    service = _service(tmp_path)

    session = service.create_session("telegram")
    sessions = service.sessions()

    assert session.plugin_id == "telegram"
    assert session.session_id in sessions
    assert sessions[session.session_id].current_step_id == "overview"


def test_marketplace_service_install_interactive_respects_choice_conditions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="demo",
        title="Demo",
        summary="demo",
        steps=(
            OnboardingStep(
                id="mode",
                kind="choice",
                title="Mode",
                result_key="mode",
                options=(
                    OnboardingOption(value="websocket", label="WebSocket"),
                    OnboardingOption(value="webhook", label="Webhook"),
                ),
            ),
            OnboardingStep(
                id="webhook",
                kind="form",
                title="Webhook details",
                fields=(OnboardingField(key="webhook_path", title="Webhook path"),),
                when=(OnboardingCondition(key="mode", equals="webhook"),),
            ),
            OnboardingStep(
                id="socket",
                kind="form",
                title="Socket details",
                fields=(OnboardingField(key="socket_enabled", title="Socket enabled", kind="bool", default=True),),
                when=(OnboardingCondition(key="mode", equals="websocket"),),
            ),
            OnboardingStep(id="validate", kind="validate", title="Validate"),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(choice_values=["websocket"], field_values=[True])

    state = service.install_interactive("demo", renderer=renderer)

    assert state.config["mode"] == "websocket"
    assert state.config["socket_enabled"] is True
    assert "webhook_path" not in state.config


def test_marketplace_service_install_interactive_supports_multi_choice_and_contains_conditions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="multi",
        title="Multi",
        summary="multi",
        steps=(
            OnboardingStep(
                id="providers",
                kind="multi_choice",
                title="Providers",
                result_key="enabled_providers",
                options=(
                    OnboardingOption(value="tavily", label="Tavily"),
                    OnboardingOption(value="google_pse", label="Google PSE"),
                ),
            ),
            OnboardingStep(
                id="tavily_secret",
                kind="secret_input",
                title="Tavily",
                fields=(OnboardingField(key="tavily_api_key", title="Tavily API key"),),
                when=(OnboardingCondition(key="enabled_providers", contains="tavily"),),
            ),
            OnboardingStep(
                id="google_secret",
                kind="secret_input",
                title="Google",
                fields=(OnboardingField(key="google_pse_api_key", title="Google PSE API key"),),
                when=(OnboardingCondition(key="enabled_providers", contains="google_pse"),),
            ),
            OnboardingStep(id="validate", kind="validate", title="Validate"),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(
        multi_choice_values=[["tavily"]],
        secret_values=["tvly-key"],
    )

    state = service.install_interactive("multi", renderer=renderer)

    assert state.config["enabled_providers"] == ["tavily"]
    assert service.resolve_secret("multi", "tavily_api_key") == "tvly-key"
    assert service.resolve_secret("multi", "google_pse_api_key") is None


def test_marketplace_service_install_interactive_skips_optional_step_when_declined(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="optional",
        title="Optional",
        summary="optional",
        steps=(
            OnboardingStep(
                id="core",
                kind="form",
                title="Core",
                fields=(OnboardingField(key="model", title="Model"),),
            ),
            OnboardingStep(
                id="advanced",
                kind="form",
                title="Advanced",
                description="Configure advanced options.",
                skippable=True,
                fields=(OnboardingField(key="api_base", title="API base", required=False),),
            ),
            OnboardingStep(id="validate", kind="validate", title="Validate"),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(field_values=["openai:gpt-5"], confirm_values=[False])

    state = service.install_interactive("optional", renderer=renderer)

    assert state.config["model"] == "openai:gpt-5"
    assert "api_base" not in state.config


def test_marketplace_service_install_interactive_reorders_values_from_source_key(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="reorder",
        title="Reorder",
        summary="reorder",
        steps=(
            OnboardingStep(
                id="providers",
                kind="multi_choice",
                title="Providers",
                result_key="enabled_providers",
                options=(
                    OnboardingOption(value="tavily", label="Tavily"),
                    OnboardingOption(value="google_pse", label="Google PSE"),
                    OnboardingOption(value="serper", label="Serper"),
                ),
            ),
            OnboardingStep(
                id="order",
                kind="reorder",
                title="Order",
                result_key="default_order",
                source_key="enabled_providers",
                options=(
                    OnboardingOption(value="tavily", label="Tavily"),
                    OnboardingOption(value="google_pse", label="Google PSE"),
                    OnboardingOption(value="serper", label="Serper"),
                ),
            ),
            OnboardingStep(id="validate", kind="validate", title="Validate"),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(
        multi_choice_values=[["tavily", "serper"]],
        reorder_values=[["serper"]],
    )

    state = service.install_interactive("reorder", renderer=renderer)

    assert state.config["enabled_providers"] == ["tavily", "serper"]
    assert state.config["default_order"] == ["serper", "tavily"]


def test_move_reorder_value_swaps_selected_item_with_neighbor() -> None:
    updated, index = _move_reorder_value(["tavily", "google_pse", "serper"], "google_pse", -1)

    assert updated == ["google_pse", "tavily", "serper"]
    assert index == 0

    updated, index = _move_reorder_value(["tavily", "google_pse", "serper"], "google_pse", 1)

    assert updated == ["tavily", "serper", "google_pse"]
    assert index == 2


def test_marketplace_service_install_interactive_reviews_summary_before_install(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="summary",
        title="Summary",
        summary="summary",
        secret_requirements=(SecretRequirement(key="api_key", title="API key", required=False),),
        steps=(
            OnboardingStep(
                id="config",
                kind="form",
                title="Config",
                fields=(OnboardingField(key="model", title="Model"),),
            ),
            OnboardingStep(
                id="secret",
                kind="secret_input",
                title="Secret",
                fields=(OnboardingField(key="api_key", title="API key", required=False),),
            ),
            OnboardingStep(id="validate", kind="validate", title="Validate"),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(field_values=["openai:gpt-5"], secret_values=["sk-test"])

    state = service.install_interactive("summary", renderer=renderer)

    assert state.config["model"] == "openai:gpt-5"
    assert renderer.review_calls
    title, text = renderer.review_calls[0]
    assert title == "Summary Summary"
    assert "Config:" in text
    assert "- Model: openai:gpt-5" in text
    assert "Secret:" in text
    assert "- API key: updated in this install" in text


def test_marketplace_service_summary_uses_custom_labels_and_templates(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="labels",
        title="Labels",
        summary="labels",
        steps=(
            OnboardingStep(
                id="provider",
                kind="choice",
                title="Provider",
                result_key="provider",
                summary_label="Primary provider",
                options=(
                    OnboardingOption(value="openrouter", label="OpenRouter"),
                    OnboardingOption(value="openai", label="OpenAI"),
                ),
            ),
            OnboardingStep(
                id="config",
                kind="form",
                title="Config",
                fields=(
                    OnboardingField(
                        key="model",
                        title="Model",
                        summary_label="Model id",
                        summary_template="{label} => {value}",
                    ),
                ),
            ),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(choice_values=["openrouter"], field_values=["openai:gpt-5"])
    renderer.review_values = [ReviewSelection(action="install")]

    state = service.install_interactive("labels", renderer=renderer)

    assert state.config["provider"] == "openrouter"
    _, text = renderer.review_calls[0]
    assert "- Primary provider: OpenRouter" in text
    assert "- Model id => openai:gpt-5" in text


def test_marketplace_service_install_interactive_allows_editing_from_summary(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="edit",
        title="Edit",
        summary="edit",
        steps=(
            OnboardingStep(
                id="config",
                kind="form",
                title="Config",
                fields=(OnboardingField(key="model", title="Model"),),
            ),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(field_values=["openai:gpt-5", "openai:gpt-4.1"])
    renderer.review_values = [
        ReviewSelection(action="edit", step_id="config"),
        ReviewSelection(action="install"),
    ]

    state = service.install_interactive("edit", renderer=renderer)

    assert state.config["model"] == "openai:gpt-4.1"
    assert len(renderer.review_calls) == 2


def test_render_manifest_includes_step_options_defaults_and_summary_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="showcase",
        title="Showcase",
        summary="showcase",
        steps=(
            OnboardingStep(
                id="providers",
                kind="multi_choice",
                title="Providers",
                result_key="enabled_providers",
                summary_label="Enabled providers",
                scenario_hint="Enable both for fallback.",
                options=(
                    OnboardingOption(
                        value="tavily",
                        label="Tavily",
                        description="Recommended",
                        recommended=True,
                        recommendation_reason="Best default.",
                    ),
                    OnboardingOption(value="google_pse", label="Google PSE"),
                ),
            ),
            OnboardingStep(
                id="order",
                kind="reorder",
                title="Provider priority",
                result_key="default_order",
                source_key="enabled_providers",
                skippable=True,
                summary_label="Provider priority",
                options=(
                    OnboardingOption(value="tavily", label="Tavily"),
                    OnboardingOption(value="google_pse", label="Google PSE"),
                ),
            ),
            OnboardingStep(
                id="advanced",
                kind="form",
                title="Advanced",
                fields=(
                    OnboardingField(
                        key="timeout_seconds",
                        title="Timeout seconds",
                        kind="int",
                        default=15,
                        required=False,
                        recommended_value=15,
                        recommendation_reason="Good default.",
                        scenario_hint="Raise it for slower providers.",
                        summary_label="Timeout",
                        summary_template="{label} => {value}",
                    ),
                ),
            ),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])

    rendered = service.render_manifest("showcase")

    assert "steps:" in rendered
    assert "- providers: multi_choice :: Providers" in rendered
    assert "scenario_hint: Enable both for fallback." in rendered
    assert "result_key: enabled_providers" in rendered
    assert "summary_label: Enabled providers" in rendered
    assert "options:" in rendered
    assert "- tavily: Tavily (Recommended) [recommended]" in rendered
    assert "recommendation_reason: Best default." in rendered
    assert "- order: reorder :: Provider priority" in rendered
    assert "source_key: enabled_providers" in rendered
    assert "skippable: true" in rendered
    assert "- timeout_seconds: Timeout seconds [int | optional | default=15 | recommended=15]" in rendered
    assert "recommendation_reason: Good default." in rendered
    assert "scenario_hint: Raise it for slower providers." in rendered
    assert "summary_template: {label} => {value}" in rendered


def test_status_lines_include_grouped_current_state_and_issues(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="statusy",
        title="Statusy",
        summary="statusy",
        secret_requirements=(SecretRequirement(key="api_key", title="API key"),),
        steps=(
            OnboardingStep(
                id="provider",
                kind="choice",
                title="Provider",
                result_key="provider",
                summary_label="Primary provider",
                options=(OnboardingOption(value="openai", label="OpenAI"),),
            ),
            OnboardingStep(
                id="config",
                kind="form",
                title="Config",
                fields=(OnboardingField(key="model", title="Model"),),
            ),
            OnboardingStep(
                id="secret",
                kind="secret_input",
                title="Secret",
                fields=(OnboardingField(key="api_key", title="API key"),),
            ),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    service.install("statusy", config_updates={"provider": "openai", "model": "gpt-5"})

    lines = service.status_lines("statusy")
    rendered = "\n".join(lines)

    assert "current_state:" in rendered
    assert "Provider:" in rendered
    assert "- Primary provider: OpenAI" in rendered
    assert "Config:" in rendered
    assert "- Model: gpt-5" in rendered
    assert "issues:" in rendered
    assert "- error: API key is required." in rendered


def test_renderer_step_guidance_and_option_lines_include_recommendations() -> None:
    step = OnboardingStep(
        id="provider",
        kind="choice",
        title="Provider",
        description="Choose one provider.",
        scenario_hint="Use OpenRouter first for a quick setup.",
        options=(
            OnboardingOption(
                value="openrouter",
                label="OpenRouter",
                description="Recommended default",
                recommended=True,
                recommendation_reason="Broad model coverage with one key.",
            ),
            OnboardingOption(value="openai", label="OpenAI"),
        ),
    )

    guidance = _step_guidance_text(step)
    option_lines = _render_typer_option_lines(step.options)

    assert "Choose one provider." in guidance
    assert "Scenario: Use OpenRouter first for a quick setup." in guidance
    assert "1. OpenRouter [recommended] - Recommended default" in option_lines
    assert "   why: Broad model coverage with one key." in option_lines


def test_renderer_prioritizes_recommended_options_and_defaults_to_them() -> None:
    options = [
        OnboardingOption(value="openai", label="OpenAI"),
        OnboardingOption(value="openrouter", label="OpenRouter", recommended=True),
        OnboardingOption(value="anthropic", label="Anthropic"),
    ]

    prioritized = _prioritize_options(options)

    assert [option.value for option in prioritized] == ["openrouter", "openai", "anthropic"]
    assert _default_option_value(prioritized, None) == "openrouter"
    assert _default_option_value(prioritized, "anthropic") == "anthropic"


def test_renderer_for_surface_can_build_cli_renderer(monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("sys.stdout", SimpleNamespace(isatty=lambda: True))

    renderer = renderer_for_surface("cli")

    assert renderer.__class__.__name__ == "CliOnboardingRenderer"


def test_marketplace_service_uses_renderer_run_install_when_available(tmp_path: Path) -> None:
    service = _service(tmp_path)

    class _WizardRenderer:
        def run_install(self, **kwargs):
            return "sentinel"

    result = service.install_interactive("telegram", renderer=_WizardRenderer())

    assert result == "sentinel"


def test_renderer_field_guidance_includes_recommended_value_reason_and_scenario() -> None:
    field = OnboardingField(
        key="timeout_seconds",
        title="Timeout seconds",
        kind="int",
        recommended_value=15,
        recommendation_reason="Good balance for normal network latency.",
        scenario_hint="Raise this only if your provider is consistently slow.",
        example="15",
    )

    guidance = _field_guidance_text(field)

    assert "Recommended: 15" in guidance
    assert "Why: Good balance for normal network latency." in guidance
    assert "Scenario: Raise this only if your provider is consistently slow." in guidance
    assert "Example: 15" in guidance


class _ValidatedConfig(BaseModel):
    timeout_seconds: int = Field(default=1, gt=0)
    api_format: str = "completion"
    api_base: str | None = None

    @model_validator(mode="after")
    def _validate_api_base(self) -> _ValidatedConfig:
        if self.api_format == "responses" and self.api_base == "https://example.com/chat-completions":
            raise ValueError("api_base is incompatible with responses")
        return self


class _ValidatedOrderConfig(BaseModel):
    enabled_providers: list[str] = Field(default_factory=list)
    default_order: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_order(self) -> _ValidatedOrderConfig:
        unknown = [item for item in self.default_order if item not in self.enabled_providers]
        if unknown:
            raise ValueError("default_order contains provider(s) that are not enabled")
        return self


def test_marketplace_service_retries_current_form_step_on_validation_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="validated",
        title="Validated",
        summary="validated",
        config_model=_ValidatedConfig,
        steps=(
            OnboardingStep(
                id="runtime",
                kind="form",
                title="Runtime",
                fields=(OnboardingField(key="timeout_seconds", title="Timeout seconds", kind="int"),),
            ),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(field_values=[0, 15])
    renderer.review_values = [ReviewSelection(action="install")]

    state = service.install_interactive("validated", renderer=renderer)

    assert state.config["timeout_seconds"] == 15
    assert renderer.error_calls
    assert "Timeout seconds must be greater than 0." in renderer.error_calls[0][1]


def test_marketplace_service_retries_secret_step_on_validation_error(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="validated_secret",
        title="Validated Secret",
        summary="validated",
        config_model=_ValidatedConfig,
        steps=(
            OnboardingStep(
                id="api",
                kind="form",
                title="API",
                fields=(OnboardingField(key="api_format", title="API format"),),
            ),
            OnboardingStep(
                id="secret_like",
                kind="secret_input",
                title="API base",
                fields=(OnboardingField(key="api_base", title="API base", required=False),),
            ),
        ),
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    renderer = StubRenderer(field_values=["responses"], secret_values=["https://example.com/chat-completions", "https://example.com/root"])
    renderer.review_values = [ReviewSelection(action="install")]

    state = service.install_interactive("validated_secret", renderer=renderer)

    assert state.config["api_format"] == "responses"
    assert service.resolve_secret("validated_secret", "api_base") == "https://example.com/root"
    assert renderer.error_calls
    assert "This API base cannot be used with API format 'responses'." in renderer.error_calls[0][1]


def test_validate_model_humanizes_missing_secret_and_global_order_errors(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    manifest = OnboardingManifest(
        plugin_id="humanized",
        title="Humanized",
        summary="humanized",
        config_model=_ValidatedOrderConfig,
    )
    service = MarketplaceService(workspace=workspace, home=home, manifests=[manifest])
    service.install("humanized", config_updates={"enabled_providers": ["tavily"], "default_order": ["google_pse"]})

    report = service.validate("humanized")
    messages = [issue.message for issue in report.issues]

    assert "Provider priority can only include search providers that are enabled. Go back and enable them first." in messages
