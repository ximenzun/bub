from __future__ import annotations

import pytest

from bub.builtin.agent import _build_llm, _provider_from_model, _provider_supports_responses, _use_responses
from bub.builtin.settings import AgentSettings


def test_provider_supports_responses_matches_provider_capabilities() -> None:
    assert _provider_supports_responses("openai") is True
    assert _provider_supports_responses("openrouter") is False


def test_provider_from_model_extracts_prefix() -> None:
    assert _provider_from_model("openai:gpt-4.1-mini") == "openai"
    assert _provider_from_model("openrouter:qwen/qwen3-coder-next") == "openrouter"


def test_use_responses_true_for_auto_and_responses() -> None:
    assert _use_responses("auto") is True
    assert _use_responses("responses") is True
    assert _use_responses("chat") is False
    assert _use_responses("anthropic") is False
    assert _use_responses("gemini") is False


def test_build_llm_enables_responses_for_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class DummyLLM:
        def __init__(self, model: str, **kwargs) -> None:
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr("bub.builtin.agent.LLM", DummyLLM)

    settings = AgentSettings(model="openai:gpt-4.1-mini", api_mode="auto")

    _build_llm(settings, tape_store=object())  # type: ignore[arg-type]

    assert captured["model"] == "openai:gpt-4.1-mini"
    assert captured["use_responses"] is True


@pytest.mark.parametrize(
    ("model", "api_mode"),
    [
        ("openai:gpt-4.1-mini", "chat"),
        ("anthropic:claude-sonnet-4-5", "anthropic"),
        ("gemini:gemini-2.5-pro", "gemini"),
    ],
)
def test_build_llm_disables_responses_for_non_responses_modes(
    monkeypatch: pytest.MonkeyPatch, model: str, api_mode: str
) -> None:
    captured: dict[str, object] = {}

    class DummyLLM:
        def __init__(self, model: str, **kwargs) -> None:
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr("bub.builtin.agent.LLM", DummyLLM)

    settings = AgentSettings(model=model, api_mode=api_mode)

    _build_llm(settings, tape_store=object())  # type: ignore[arg-type]

    assert captured["use_responses"] is False


def test_build_llm_rejects_explicit_responses_for_unsupported_provider() -> None:
    settings = AgentSettings(model="openrouter:qwen/qwen3-coder-next", api_mode="responses")

    with pytest.raises(RuntimeError, match="api_mode='responses' is not supported"):
        _build_llm(settings, tape_store=object())  # type: ignore[arg-type]


def test_build_llm_rejects_anthropic_mode_for_non_anthropic_provider() -> None:
    settings = AgentSettings(model="openai:gpt-4.1-mini", api_mode="anthropic")

    with pytest.raises(RuntimeError, match="api_mode='anthropic' requires an Anthropic-compatible provider"):
        _build_llm(settings, tape_store=object())  # type: ignore[arg-type]


def test_build_llm_rejects_gemini_mode_for_non_gemini_provider() -> None:
    settings = AgentSettings(model="anthropic:claude-sonnet-4-5", api_mode="gemini")

    with pytest.raises(RuntimeError, match="api_mode='gemini' requires the Gemini provider"):
        _build_llm(settings, tape_store=object())  # type: ignore[arg-type]
