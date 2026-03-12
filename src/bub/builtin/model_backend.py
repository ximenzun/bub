"""Default model backend powered by republic + any-llm."""

from __future__ import annotations

from typing import Literal

from any_llm import AnyLLM
from republic import LLM, AsyncTapeStore
from republic.tape import TapeContext

from bub.builtin.settings import AgentSettings, ApiMode

type RepublicApiFormat = Literal["completion", "responses", "messages"]


class RepublicModelBackend:
    """Default backend that builds republic LLM clients from Bub settings."""

    def build_llm(self, *, settings: AgentSettings, tape_store: AsyncTapeStore, context: TapeContext) -> LLM:
        return LLM(
            settings.model,
            api_key=settings.api_key,
            api_base=settings.api_base,
            fallback_models=[settings.fallback_model] if settings.fallback_model else None,
            api_format=_api_format_for_model(settings.model, settings.api_mode),
            tape_store=tape_store,
            context=context,
        )


def _api_format_for_model(model: str, api_mode: ApiMode) -> RepublicApiFormat:
    provider = _provider_from_model(model)
    if api_mode == "chat":
        return "completion"
    if api_mode == "responses":
        if not _provider_supports_responses_transport(provider):
            raise RuntimeError(_api_mode_error(api_mode, provider))
        return "responses"
    if api_mode == "native":
        return _native_api_format(provider)
    if _provider_supports_responses_transport(provider):
        return "responses"
    if _provider_supports_messages(provider):
        return "messages"
    return "completion"


def _provider_from_model(model: str) -> str:
    provider, _sep, _rest = model.partition(":")
    return provider if provider else model


def _provider_supports_responses(provider: str) -> bool:
    try:
        provider_class = AnyLLM.get_provider_class(provider)
    except Exception:
        return False
    return bool(getattr(provider_class, "SUPPORTS_RESPONSES", False))


def _provider_supports_responses_transport(provider: str) -> bool:
    # republic 0.5.4 explicitly enables OpenRouter responses even though any-llm
    # still reports SUPPORTS_RESPONSES=False for that provider.
    return provider == "openrouter" or _provider_supports_responses(provider)


def _provider_supports_messages(provider: str) -> bool:
    return provider in {"anthropic", "vertexaianthropic"}


def _native_api_format(provider: str) -> RepublicApiFormat:
    if _provider_supports_messages(provider):
        return "messages"
    if _provider_supports_responses_transport(provider):
        return "responses"
    return "completion"


def _api_mode_error(api_mode: ApiMode, provider: str) -> str:
    if api_mode == "responses":
        return (
            f"api_mode='responses' is not supported for provider '{provider}'. "
            "Use BUB_API_MODE=auto, BUB_API_MODE=native, or BUB_API_MODE=chat instead."
        )
    return f"api_mode='{api_mode}' is not supported for provider '{provider}'."
