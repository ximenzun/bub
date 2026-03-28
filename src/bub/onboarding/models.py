from __future__ import annotations

import importlib.metadata
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from bub.onboarding.service import MarketplaceService
    from bub.social.capabilities import ChannelCapabilities

type OnboardingSurface = Literal[
    "cli",
    "chat_card",
    "chat_text",
    "chat_image",
    "web_modal",
    "browser_open",
    "terminal_qr",
]
type OnboardingStepKind = Literal[
    "info",
    "choice",
    "multi_choice",
    "reorder",
    "form",
    "secret_input",
    "external_link",
    "qr_challenge",
    "validate",
    "complete",
]
type OnboardingFieldKind = Literal["string", "int", "bool", "json", "string_list"]
type PluginCategory = Literal["agent", "channel", "connector", "plugin", "service"]
type PortabilityMode = Literal["none", "portable", "portable_encrypted", "refs_only", "rebind_required", "non_portable"]


class SecretRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["file"] = "file"
    ref: str
    plugin_id: str
    key: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SecretRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    description: str = ""
    required: bool = True
    example: str | None = None
    multiline: bool = False


class OnboardingOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    label: str
    description: str = ""
    recommended: bool = False
    recommendation_reason: str = ""


class OnboardingField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    kind: OnboardingFieldKind = "string"
    description: str = ""
    required: bool = True
    placeholder: str | None = None
    example: str | None = None
    default: Any = None
    recommended_value: Any = None
    recommendation_reason: str = ""
    scenario_hint: str = ""
    summary_label: str | None = None
    summary_template: str | None = None
    when: tuple[OnboardingCondition, ...] = ()


class OnboardingCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    equals: Any = None
    one_of: tuple[Any, ...] = ()
    contains: Any = None
    present: bool | None = None

    def matches(self, values: dict[str, Any]) -> bool:
        present = self.key in values and values[self.key] not in (None, "", [], (), {})
        if self.present is not None and present is not self.present:
            return False
        if self.equals is not None:
            return bool(values.get(self.key) == self.equals)
        if self.one_of:
            return bool(values.get(self.key) in self.one_of)
        if self.contains is not None:
            current = values.get(self.key)
            if isinstance(current, (list, tuple, set)):
                return bool(self.contains in current)
            return False
        return present


class OnboardingStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: OnboardingStepKind
    title: str
    description: str = ""
    surfaces: tuple[OnboardingSurface, ...] = ()
    fields: tuple[OnboardingField, ...] = ()
    options: tuple[OnboardingOption, ...] = ()
    external_url: str | None = None
    external_label: str | None = None
    skippable: bool = False
    result_key: str | None = None
    source_key: str | None = None
    scenario_hint: str = ""
    summary_label: str | None = None
    summary_template: str | None = None
    when: tuple[OnboardingCondition, ...] = ()


class PluginTestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    mode: Literal["automated", "manual"] = "manual"
    description: str = ""
    commands: tuple[str, ...] = ()
    assertions: tuple[str, ...] = ()


class PluginInstallState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    title: str
    enabled: bool = True
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, SecretRef] = Field(default_factory=dict)
    installed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    installed_via: OnboardingSurface = "cli"
    channel_name: str | None = None
    last_validation_ok: bool | None = None
    last_validation_summary: str | None = None
    last_validation_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: Literal["info", "warning", "error"]
    message: str


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    summary: str
    issues: list[ValidationIssue] = Field(default_factory=list)


class OnboardingSessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    plugin_id: str
    surface: OnboardingSurface
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    current_step_id: str | None = None
    completed_step_ids: list[str] = Field(default_factory=list)
    answers: dict[str, Any] = Field(default_factory=dict)
    done: bool = False


class PortabilityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config: PortabilityMode = "portable"
    secrets: PortabilityMode = "portable_encrypted"
    runtime_state: PortabilityMode = "rebind_required"
    tapes: PortabilityMode = "portable"


@dataclass(frozen=True, slots=True)
class InstallContext:
    workspace: Path
    service: MarketplaceService
    manifest: OnboardingManifest
    state: PluginInstallState | None


type ManifestValidator = Callable[[InstallContext], ValidationReport]
type RuntimeFactory = Callable[[InstallContext], Any]
type LegacyEnvFactory = Callable[[InstallContext], dict[str, str]]


@dataclass(frozen=True, slots=True)
class OnboardingManifest:
    plugin_id: str
    title: str
    summary: str
    category: PluginCategory = "plugin"
    description: str = ""
    channel_name: str | None = None
    entry_point_name: str | None = None
    package_name: str | None = None
    config_model: type[BaseModel] | None = None
    steps: tuple[OnboardingStep, ...] = ()
    secret_requirements: tuple[SecretRequirement, ...] = ()
    test_plan: tuple[PluginTestCase, ...] = ()
    surfaces: tuple[OnboardingSurface, ...] = ("cli",)
    capability_tags: tuple[str, ...] = ()
    legacy_env_vars: tuple[str, ...] = ()
    builtin: bool = False
    capabilities: ChannelCapabilities | None = None
    validator: ManifestValidator | None = None
    runtime_factory: RuntimeFactory | None = None
    legacy_env_factory: LegacyEnvFactory | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    portability: PortabilityPolicy = field(default_factory=PortabilityPolicy)

    def config_schema(self) -> dict[str, Any]:
        if self.config_model is None:
            return {}
        return self.config_model.model_json_schema()

    def config_defaults(self) -> dict[str, Any]:
        if self.config_model is None:
            return {}
        payload = self.config_model.model_construct().model_dump(mode="json", exclude_none=True)
        return dict(payload)

    def runtime_is_available(self) -> bool:
        if self.builtin:
            return True
        if self.package_name is not None:
            try:
                return importlib.metadata.version(self.package_name) is not None
            except importlib.metadata.PackageNotFoundError:
                return False
        if self.entry_point_name is None:
            return True
        candidates = importlib.metadata.entry_points(group="bub")
        return any(entry_point.name == self.entry_point_name for entry_point in candidates)

    def create_session(self, *, surface: OnboardingSurface) -> OnboardingSessionRecord:
        now = datetime.now(UTC)
        first_step = self.steps[0].id if self.steps else None
        return OnboardingSessionRecord(
            session_id=f"onb_{self.plugin_id}_{secrets.token_hex(8)}",
            plugin_id=self.plugin_id,
            surface=surface,
            created_at=now,
            updated_at=now,
            current_step_id=first_step,
        )
