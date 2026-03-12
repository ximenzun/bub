from __future__ import annotations

import pytest
from pydantic import ValidationError

from bub.builtin.agent import _build_llm
from bub.builtin.model_backend import (
    _api_format_for_model,
    _native_api_format,
    _provider_from_model,
    _provider_supports_responses,
    _provider_supports_responses_transport,
)
from bub.builtin.settings import AgentSettings


def test_provider_supports_responses_matches_provider_capabilities() -> None:
    assert _provider_supports_responses("openai") is True
    assert _provider_supports_responses("openrouter") is False


def test_provider_supports_responses_transport_includes_openrouter_policy() -> None:
    assert _provider_supports_responses_transport("openai") is True
    assert _provider_supports_responses_transport("openrouter") is True
    assert _provider_supports_responses_transport("anthropic") is False


def test_provider_from_model_extracts_prefix() -> None:
    assert _provider_from_model("openai:gpt-4.1-mini") == "openai"
    assert _provider_from_model("openrouter:qwen/qwen3-coder-next") == "openrouter"


def test_native_api_format_prefers_provider_native_transport() -> None:
    assert _native_api_format("openai") == "responses"
    assert _native_api_format("openrouter") == "responses"
    assert _native_api_format("anthropic") == "messages"
    assert _native_api_format("gemini") == "completion"


def test_api_format_for_model_matches_transport_mode_and_provider_capabilities() -> None:
    assert _api_format_for_model("openai:gpt-4.1-mini", "auto") == "responses"
    assert _api_format_for_model("openrouter:qwen/qwen3-coder-next", "auto") == "responses"
    assert _api_format_for_model("openai:gpt-4.1-mini", "responses") == "responses"
    assert _api_format_for_model("openai:gpt-4.1-mini", "native") == "responses"
    assert _api_format_for_model("openai:gpt-4.1-mini", "chat") == "completion"
    assert _api_format_for_model("anthropic:claude-sonnet-4-5", "native") == "messages"
    assert _api_format_for_model("gemini:gemini-2.5-pro", "native") == "completion"


def test_build_llm_uses_api_format_for_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class DummyLLM:
        def __init__(self, model: str, **kwargs) -> None:
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr("bub.builtin.model_backend.LLM", DummyLLM)

    settings = AgentSettings(model="openai:gpt-4.1-mini", api_mode="auto")

    _build_llm(settings, tape_store=object())  # type: ignore[arg-type]

    assert captured["model"] == "openai:gpt-4.1-mini"
    assert captured["api_format"] == "responses"


@pytest.mark.parametrize(
    ("model", "api_mode", "expected_api_format"),
    [
        ("openai:gpt-4.1-mini", "chat", "completion"),
        ("openai:gpt-4.1-mini", "native", "responses"),
        ("anthropic:claude-sonnet-4-5", "native", "messages"),
        ("gemini:gemini-2.5-pro", "native", "completion"),
    ],
)
def test_build_llm_sets_api_format_from_backend_transport_mode(
    monkeypatch: pytest.MonkeyPatch, model: str, api_mode: str, expected_api_format: str
) -> None:
    captured: dict[str, object] = {}

    class DummyLLM:
        def __init__(self, model: str, **kwargs) -> None:
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr("bub.builtin.model_backend.LLM", DummyLLM)

    settings = AgentSettings(model=model, api_mode=api_mode)

    _build_llm(settings, tape_store=object())  # type: ignore[arg-type]

    assert captured["api_format"] == expected_api_format


def test_build_llm_rejects_explicit_responses_for_unsupported_provider() -> None:
    settings = AgentSettings(model="anthropic:claude-sonnet-4-5", api_mode="responses")

    with pytest.raises(RuntimeError, match="api_mode='responses' is not supported"):
        _build_llm(settings, tape_store=object())  # type: ignore[arg-type]


def test_build_llm_includes_fallback_model_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class DummyLLM:
        def __init__(self, model: str, **kwargs) -> None:
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr("bub.builtin.model_backend.LLM", DummyLLM)

    settings = AgentSettings(
        model="openai:gpt-4.1-mini",
        fallback_model="openai:gpt-4.1-nano",
        api_mode="auto",
    )

    _build_llm(settings, tape_store=object())  # type: ignore[arg-type]

    assert captured["fallback_models"] == ["openai:gpt-4.1-nano"]


def test_agent_settings_rejects_legacy_provider_modes() -> None:
    with pytest.raises(ValidationError, match="api_mode"):
        AgentSettings(model="anthropic:claude-sonnet-4-5", api_mode="anthropic")  # type: ignore[arg-type]

    with pytest.raises(ValidationError, match="api_mode"):
        AgentSettings(model="gemini:gemini-2.5-pro", api_mode="gemini")  # type: ignore[arg-type]


def test_build_llm_uses_custom_backend() -> None:
    settings = AgentSettings(model="openai:gpt-4.1-mini", api_mode="auto")
    captured: dict[str, object] = {}

    class DummyBackend:
        def build_llm(self, *, settings, tape_store, context):
            captured["settings"] = settings
            captured["tape_store"] = tape_store
            captured["context"] = context
            return "llm"

    llm = _build_llm(settings, tape_store=object(), backend=DummyBackend())  # type: ignore[arg-type]

    assert llm == "llm"
    assert captured["settings"] == settings
