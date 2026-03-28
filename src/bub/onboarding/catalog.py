from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

from bub.onboarding.models import (
    InstallContext,
    OnboardingCondition,
    OnboardingField,
    OnboardingManifest,
    OnboardingOption,
    OnboardingStep,
    PluginTestCase,
    PortabilityPolicy,
    SecretRequirement,
)


class TelegramConfig(BaseModel):
    enabled: bool = True
    allow_users: list[str] = Field(default_factory=list)
    allow_chats: list[str] = Field(default_factory=list)
    proxy: str | None = None


class AgentRuntimeConfig(BaseModel):
    enabled: bool = True
    primary_provider: str | None = None
    model: str = "openrouter:qwen/qwen3-coder-next"
    fallback_model: str | None = None
    api_format: str = "completion"
    max_steps: int = 50
    max_tokens: int = 1024
    model_timeout_seconds: int | None = None
    openrouter_api_base: str | None = None
    openai_api_base: str | None = None
    anthropic_api_base: str | None = None

    @model_validator(mode="after")
    def _validate_api_shape(self) -> AgentRuntimeConfig:
        if self.api_format != "responses":
            return self
        for key in ("openrouter_api_base", "openai_api_base", "anthropic_api_base"):
            raw = getattr(self, key)
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = urlparse(raw).path.casefold()
            if "chat-completions" in path:
                raise ValueError(
                    f"{key} points at a chat-completions style base URL that is incompatible with api_format=responses."
                )
        return self


class ChannelManagerRuntimeConfig(BaseModel):
    enabled: bool = True
    enabled_channels: list[str] = Field(default_factory=lambda: ["all"])
    debounce_seconds: float = 1.0
    max_wait_seconds: float = 10.0
    active_time_window: float = 60.0


def _telegram_runtime(context: InstallContext) -> dict[str, Any]:
    state = context.state
    if state is None:
        return {}
    return {
        "token": context.service.resolve_secret("telegram", "bot_token") or "",
        "allow_users": ",".join(state.config.get("allow_users", [])),
        "allow_chats": ",".join(state.config.get("allow_chats", [])),
        "proxy": state.config.get("proxy"),
    }


def _agent_runtime(context: InstallContext) -> dict[str, Any]:
    state = context.state
    if state is None:
        return {}
    payload = dict(state.config)
    provider_keys = _provider_secret_map(context)
    if provider_keys:
        payload["api_key"] = provider_keys
    provider_bases = _provider_base_map(state.config)
    if provider_bases:
        payload["api_base"] = provider_bases
    return payload


def _core_agent_env(context: InstallContext) -> dict[str, str]:
    state = context.state
    if state is None:
        return {}
    config = state.config
    env = _env_from_scalar_map(
        config,
        {
            "model": "BUB_MODEL",
            "fallback_model": "BUB_FALLBACK_MODEL",
            "api_format": "BUB_API_FORMAT",
            "max_steps": "BUB_MAX_STEPS",
            "max_tokens": "BUB_MAX_TOKENS",
            "model_timeout_seconds": "BUB_MODEL_TIMEOUT_SECONDS",
        },
    )
    for provider, value in _provider_secret_map(context).items():
        env[f"BUB_{provider.upper()}_API_KEY"] = value
    for provider, value in _provider_base_map(config).items():
        env[f"BUB_{provider.upper()}_API_BASE"] = value
    return env


def _channel_manager_runtime(context: InstallContext) -> dict[str, Any]:
    state = context.state
    if state is None:
        return {}
    return dict(state.config)


def _channel_manager_env(context: InstallContext) -> dict[str, str]:
    state = context.state
    if state is None:
        return {}
    env = _env_from_scalar_map(
        state.config,
        {
            "debounce_seconds": "BUB_DEBOUNCE_SECONDS",
            "max_wait_seconds": "BUB_MAX_WAIT_SECONDS",
            "active_time_window": "BUB_ACTIVE_TIME_WINDOW",
        },
    )
    channels = state.config.get("enabled_channels")
    if isinstance(channels, list):
        env["BUB_ENABLED_CHANNELS"] = ",".join(str(item) for item in channels)
    return env


def _telegram_env(context: InstallContext) -> dict[str, str]:
    state = context.state
    if state is None:
        return {}
    env = {}
    token = context.service.resolve_secret("telegram", "bot_token")
    if token:
        env["BUB_TELEGRAM_TOKEN"] = token
    if state.config.get("allow_users"):
        env["BUB_TELEGRAM_ALLOW_USERS"] = ",".join(state.config["allow_users"])
    if state.config.get("allow_chats"):
        env["BUB_TELEGRAM_ALLOW_CHATS"] = ",".join(state.config["allow_chats"])
    if state.config.get("proxy"):
        env["BUB_TELEGRAM_PROXY"] = str(state.config["proxy"])
    return env


def _provider_secret_map(context: InstallContext) -> dict[str, str]:
    state = context.state
    if state is None:
        return {}
    result: dict[str, str] = {}
    for key in state.secret_refs:
        if not key.endswith("_api_key"):
            continue
        provider = key.removesuffix("_api_key")
        value = context.service.resolve_secret("agent", key)
        if value:
            result[provider] = value
    return result


def _provider_base_map(config: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in config.items():
        if not key.endswith("_api_base") or value is None:
            continue
        result[key.removesuffix("_api_base")] = str(value)
    return result


def _env_from_scalar_map(config: dict[str, Any], mapping: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, env_name in mapping.items():
        value = config.get(key)
        if value is None:
            continue
        env[env_name] = str(value).lower() if isinstance(value, bool) else str(value)
    return env


def builtin_marketplace_manifests() -> list[OnboardingManifest]:
    return [
        OnboardingManifest(
            plugin_id="agent",
            title="Agent Runtime",
            summary="Configure Bub's core model runtime.",
            category="agent",
            builtin=True,
            config_model=AgentRuntimeConfig,
            steps=(
                OnboardingStep(
                    id="provider",
                    kind="choice",
                    title="Which provider should Bub use first?",
                    description="Choose the main provider you want to configure now. You can still add fallback settings later.",
                    scenario_hint="OpenRouter is usually the fastest way to get Bub running. Pick OpenAI or Anthropic only when you already know you want those providers.",
                    result_key="primary_provider",
                    summary_label="Primary provider",
                    options=(
                        OnboardingOption(
                            value="openrouter",
                            label="OpenRouter",
                            description="Recommended general default",
                            recommended=True,
                            recommendation_reason="Best default when you want one API key and broad model coverage.",
                        ),
                        OnboardingOption(
                            value="openai",
                            label="OpenAI",
                            description="Use OpenAI-hosted models",
                            recommendation_reason="Choose this when you already standardize on OpenAI-hosted models or Responses APIs.",
                        ),
                        OnboardingOption(
                            value="anthropic",
                            label="Anthropic",
                            description="Use Claude models",
                            recommendation_reason="Choose this when Claude is your primary model family.",
                        ),
                    ),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="model",
                    kind="form",
                    title="Model selection",
                    fields=(
                        OnboardingField(
                            key="model",
                            title="Model",
                            default="openrouter:qwen/qwen3-coder-next",
                            recommended_value="openrouter:qwen/qwen3-coder-next",
                            recommendation_reason="A safe default to get Bub running before you optimize for a specific provider or latency profile.",
                            scenario_hint="Use a concrete provider-prefixed model id such as openrouter:qwen/... or openai:gpt-5.4.",
                        ),
                    ),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="api_format",
                    kind="choice",
                    title="Which API format should Bub use?",
                    description="Use completion for OpenRouter-style completions, responses for modern Responses APIs, or messages for chat-completions style APIs.",
                    scenario_hint="Leave this on completion unless you know the upstream API expects Responses or chat-completions message payloads.",
                    result_key="api_format",
                    summary_label="API format",
                    options=(
                        OnboardingOption(
                            value="completion",
                            label="completion",
                            description="Recommended default",
                            recommended=True,
                            recommendation_reason="Best starting point for OpenRouter and many OpenAI-compatible providers.",
                        ),
                        OnboardingOption(
                            value="responses",
                            label="responses",
                            description="For Responses-style APIs",
                            recommendation_reason="Use this when the provider explicitly offers a Responses API shape.",
                        ),
                        OnboardingOption(
                            value="messages",
                            label="messages",
                            description="For chat-completions style APIs",
                            recommendation_reason="Use this for chat-completions style endpoints that are not Responses-compatible.",
                        ),
                    ),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="provider_key_openrouter",
                    kind="secret_input",
                    title="OpenRouter API key",
                    fields=(OnboardingField(key="openrouter_api_key", title="OpenRouter API key", required=False),),
                    when=(OnboardingCondition(key="primary_provider", equals="openrouter"),),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="provider_key_openai",
                    kind="secret_input",
                    title="OpenAI API key",
                    fields=(OnboardingField(key="openai_api_key", title="OpenAI API key", required=False),),
                    when=(OnboardingCondition(key="primary_provider", equals="openai"),),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="provider_key_anthropic",
                    kind="secret_input",
                    title="Anthropic API key",
                    fields=(OnboardingField(key="anthropic_api_key", title="Anthropic API key", required=False),),
                    when=(OnboardingCondition(key="primary_provider", equals="anthropic"),),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="limits",
                    kind="form",
                    title="Runtime limits",
                    description="These values are usually fine as-is. Adjust them if you know you need different loop or output budgets.",
                    fields=(
                        OnboardingField(
                            key="max_steps",
                            title="Max steps",
                            kind="int",
                            default=50,
                            recommended_value=50,
                            recommendation_reason="Enough for normal multi-tool tasks without letting bad loops run too long.",
                        ),
                        OnboardingField(
                            key="max_tokens",
                            title="Max tokens",
                            kind="int",
                            default=4096,
                            recommended_value=4096,
                            recommendation_reason="A practical default before you tune for channel display budgets or model-specific limits.",
                        ),
                        OnboardingField(
                            key="model_timeout_seconds",
                            title="Model timeout seconds",
                            kind="int",
                            required=False,
                            scenario_hint="Set this only if your provider is unusually slow or your tasks routinely exceed the default network patience.",
                        ),
                    ),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="advanced",
                    kind="form",
                    title="Advanced runtime options",
                    description="Optional fallback model and provider-specific API base overrides.",
                    skippable=True,
                    fields=(
                        OnboardingField(key="fallback_model", title="Fallback model", required=False),
                        OnboardingField(key="openrouter_api_base", title="OpenRouter API base", required=False),
                        OnboardingField(key="openai_api_base", title="OpenAI API base", required=False),
                        OnboardingField(key="anthropic_api_base", title="Anthropic API base", required=False),
                    ),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(id="validate", kind="validate", title="Validate agent runtime", surfaces=("cli",)),
            ),
            secret_requirements=(
                SecretRequirement(key="openrouter_api_key", title="OpenRouter API key", required=False),
                SecretRequirement(key="openai_api_key", title="OpenAI API key", required=False),
                SecretRequirement(key="anthropic_api_key", title="Anthropic API key", required=False),
            ),
            test_plan=(
                PluginTestCase(
                    id="agent-runtime",
                    title="Configure agent runtime",
                    mode="manual",
                    commands=("uv run bub marketplace install agent", 'uv run bub run "hello"'),
                    assertions=("The selected model/provider settings should be used without editing `.env`.",),
                ),
            ),
            surfaces=("cli", "web_modal"),
            capability_tags=("core", "model_runtime"),
            legacy_env_vars=(
                "BUB_MODEL",
                "BUB_FALLBACK_MODEL",
                "BUB_API_FORMAT",
                "BUB_MAX_STEPS",
                "BUB_MAX_TOKENS",
                "BUB_MODEL_TIMEOUT_SECONDS",
                "BUB_OPENROUTER_API_KEY",
                "BUB_OPENAI_API_KEY",
                "BUB_ANTHROPIC_API_KEY",
            ),
            runtime_factory=_agent_runtime,
            legacy_env_factory=_core_agent_env,
            portability=PortabilityPolicy(
                config="portable",
                secrets="portable_encrypted",
                runtime_state="portable",
                tapes="portable",
            ),
        ),
        OnboardingManifest(
            plugin_id="channel_manager",
            title="Channel Manager",
            summary="Configure Bub's core channel manager timing and default channel set.",
            category="service",
            builtin=True,
            config_model=ChannelManagerRuntimeConfig,
            steps=(
                OnboardingStep(
                    id="config",
                    kind="form",
                    title="Channel manager settings",
                    fields=(
                        OnboardingField(
                            key="enabled_channels", title="Enabled channels", kind="string_list", default=["all"]
                        ),
                        OnboardingField(key="debounce_seconds", title="Debounce seconds", kind="int", default=1),
                        OnboardingField(key="max_wait_seconds", title="Max wait seconds", kind="int", default=10),
                        OnboardingField(key="active_time_window", title="Active time window", kind="int", default=60),
                    ),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(id="validate", kind="validate", title="Validate channel manager", surfaces=("cli",)),
            ),
            test_plan=(
                PluginTestCase(
                    id="channel-manager-runtime",
                    title="Configure channel manager defaults",
                    mode="manual",
                    commands=("uv run bub marketplace install channel_manager", "uv run bub channels list"),
                    assertions=("The saved enabled channel set should drive gateway default selection.",),
                ),
            ),
            surfaces=("cli", "web_modal"),
            capability_tags=("core", "channels"),
            legacy_env_vars=(
                "BUB_ENABLED_CHANNELS",
                "BUB_DEBOUNCE_SECONDS",
                "BUB_MAX_WAIT_SECONDS",
                "BUB_ACTIVE_TIME_WINDOW",
            ),
            runtime_factory=_channel_manager_runtime,
            legacy_env_factory=_channel_manager_env,
            portability=PortabilityPolicy(
                config="portable",
                secrets="none",
                runtime_state="portable",
                tapes="portable",
            ),
        ),
        OnboardingManifest(
            plugin_id="telegram",
            title="Telegram",
            summary="Connect Bub to Telegram via a bot token.",
            category="channel",
            description="Builtin Telegram channel with allowlists and optional proxy support.",
            channel_name="telegram",
            config_model=TelegramConfig,
            steps=(
                OnboardingStep(
                    id="overview",
                    kind="info",
                    title="Telegram onboarding",
                    description="You will provide a bot token and optional allowlists for users/chats.",
                    surfaces=("cli", "chat_card", "chat_text"),
                ),
                OnboardingStep(
                    id="bot_token",
                    kind="secret_input",
                    title="Bot token",
                    fields=(OnboardingField(key="bot_token", title="Telegram bot token"),),
                    surfaces=("cli", "web_modal"),
                ),
                OnboardingStep(
                    id="access_control",
                    kind="form",
                    title="Access control",
                    fields=(
                        OnboardingField(
                            key="allow_users",
                            title="Allowed users (comma separated)",
                            kind="string_list",
                            required=False,
                        ),
                        OnboardingField(
                            key="allow_chats",
                            title="Allowed chats (comma separated)",
                            kind="string_list",
                            required=False,
                        ),
                        OnboardingField(key="proxy", title="Proxy URL", required=False),
                    ),
                    surfaces=("cli", "chat_card", "web_modal"),
                ),
                OnboardingStep(
                    id="validate", kind="validate", title="Validate configuration", surfaces=("cli", "chat_card")
                ),
            ),
            secret_requirements=(SecretRequirement(key="bot_token", title="Telegram bot token"),),
            test_plan=(
                PluginTestCase(
                    id="telegram-cli-install",
                    title="Install Telegram via marketplace CLI",
                    mode="manual",
                    commands=("uv run bub marketplace install telegram", "uv run bub marketplace status telegram"),
                    assertions=(
                        "The plugin reports installed=true.",
                        "Validation returns ready once a token is present.",
                    ),
                ),
                PluginTestCase(
                    id="telegram-gateway",
                    title="Run gateway with Telegram enabled",
                    mode="manual",
                    commands=("uv run bub gateway --enable-channel telegram",),
                    assertions=("Startup logs should report telegram.start with proxy_enabled true/false.",),
                ),
            ),
            surfaces=("cli", "chat_card", "chat_text", "web_modal"),
            capability_tags=("channel", "remote_reply", "allowlist"),
            legacy_env_vars=(
                "BUB_TELEGRAM_TOKEN",
                "BUB_TELEGRAM_ALLOW_USERS",
                "BUB_TELEGRAM_ALLOW_CHATS",
                "BUB_TELEGRAM_PROXY",
            ),
            builtin=True,
            runtime_factory=_telegram_runtime,
            legacy_env_factory=_telegram_env,
            portability=PortabilityPolicy(
                config="portable",
                secrets="portable_encrypted",
                runtime_state="portable",
                tapes="portable",
            ),
        ),
    ]
