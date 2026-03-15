from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import republic.auth.openai_codex as openai_codex
from republic import ToolAutoResult

import bub.builtin.agent as agent_module
from bub.builtin.agent import (
    Agent,
    _continue_prompt,
    _event_prompt,
    _prompt_for_tape,
    _run_tools_with_transient_prompt,
    _run_tools_without_persisting_prompt,
)
from bub.builtin.settings import AgentSettings


def test_build_llm_passes_codex_resolver_to_republic(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    resolver = object()

    class FakeLLM:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(agent_module, "LLM", FakeLLM)
    monkeypatch.setattr(openai_codex, "openai_codex_oauth_resolver", lambda: resolver)
    monkeypatch.setattr(agent_module, "default_tape_context", lambda: "ctx")

    settings = AgentSettings(model="openai:gpt-5-codex", api_key=None, api_base=None)
    tape_store = object()

    agent_module._build_llm(settings, tape_store)

    assert captured["args"] == ("openai:gpt-5-codex",)
    assert captured["kwargs"]["api_key"] is None
    assert captured["kwargs"]["api_base"] is None
    assert captured["kwargs"]["api_key_resolver"] is resolver
    assert captured["kwargs"]["tape_store"] is tape_store
    assert captured["kwargs"]["context"] == "ctx"


# ---------------------------------------------------------------------------
# Agent.run() tests: merge_back logic and model passthrough
# ---------------------------------------------------------------------------


def _make_agent() -> Agent:
    """Build an Agent with a mocked framework, bypassing real LLM/tape init."""
    framework = MagicMock()
    framework.get_tape_store.return_value = None
    framework.get_system_prompt.return_value = ""

    with patch.object(Agent, "__init__", lambda self, fw: None):
        agent = Agent.__new__(Agent)

    agent.settings = AgentSettings(model="test:model", api_key="k", api_base="b", api_format="completion")
    agent.framework = framework
    return agent


class _ForkCapture:
    """Captures the merge_back kwarg passed to fork_tape."""

    def __init__(self) -> None:
        self.merge_back_values: list[bool] = []

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        self.merge_back_values.append(merge_back)
        yield


class _FakeTapeService:
    """Minimal TapeService stand-in for testing Agent.run()."""

    def __init__(self, fork_capture: _ForkCapture) -> None:
        self._fork = fork_capture
        self.run_tools_model: str | None = None

    def session_tape(self, session_id: str, workspace: Any) -> MagicMock:
        tape = MagicMock()
        tape.name = "test-tape"
        tape.context.state = {}

        async def fake_run_tools_async(**kwargs: Any) -> ToolAutoResult:
            self.run_tools_model = kwargs.get("model")
            return ToolAutoResult(kind="text", text="done", tool_calls=[], tool_results=[], error=None)

        tape.run_tools_async = fake_run_tools_async
        return tape

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        pass

    async def hydrate_context(self, tape, runtime_state: dict[str, object] | None = None) -> None:
        if runtime_state:
            tape.context.state.update(runtime_state)

    async def append_event(self, tape_name: str, name: str, payload: dict) -> None:
        pass

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._fork.fork_tape(tape_name, merge_back=merge_back):
            yield


@pytest.mark.asyncio
async def test_agent_run_regular_session_merges_back() -> None:
    """A regular (non-temp) session should merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    await agent.run(session_id="user/session1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [True]


@pytest.mark.asyncio
async def test_agent_run_temp_session_does_not_merge_back() -> None:
    """A temp/ session should NOT merge tape entries back."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    await agent.run(session_id="temp/abc123", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [False]


@pytest.mark.asyncio
async def test_agent_run_passes_model_to_llm() -> None:
    """The model parameter should be forwarded to run_tools_async."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    await agent.run(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"}, model="openai:gpt-4o")  # noqa: S108

    assert fake_tapes.run_tools_model == "openai:gpt-4o"


@pytest.mark.asyncio
async def test_agent_run_empty_prompt_returns_error() -> None:
    agent = _make_agent()
    agent.tapes = MagicMock()  # type: ignore[assignment]

    result = await agent.run(session_id="user/s1", prompt="", state={})

    assert result == "error: empty prompt"


@pytest.mark.asyncio
async def test_agent_run_model_defaults_to_none() -> None:
    """When model is not specified, None should be passed to run_tools_async."""
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    await agent.run(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fake_tapes.run_tools_model is None


class _ContinueThenTextTapeService(_FakeTapeService):
    def __init__(self, fork_capture: _ForkCapture) -> None:
        super().__init__(fork_capture)
        self.prompts: list[str | list[dict[str, object]]] = []
        self._call_count = 0

    def session_tape(self, session_id: str, workspace: Any) -> MagicMock:
        tape = MagicMock()
        tape.name = "test-tape"
        tape.context.state = {}

        async def fake_run_tools_async(**kwargs: Any) -> ToolAutoResult:
            self.prompts.append(kwargs.get("prompt") if "prompt" in kwargs else kwargs.get("messages"))
            self._call_count += 1
            if self._call_count == 1:
                tape.context.state["_tool_media_parts"] = [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}}
                ]
                return ToolAutoResult(kind="tools", text="", tool_calls=[{"id": "call_1"}], tool_results=[], error=None)
            return ToolAutoResult(kind="text", text="done", tool_calls=[], tool_results=[], error=None)

        tape.run_tools_async = fake_run_tools_async
        tape.read_messages_async = AsyncMock(return_value=[{"role": "user", "content": "hello"}])
        return tape


@pytest.mark.asyncio
async def test_agent_continue_prompt_includes_pending_tool_media_parts() -> None:
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _ContinueThenTextTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert result == "done"
    assert fake_tapes.prompts[0] == "hello"
    assert fake_tapes.prompts[1] == [
        {"type": "text", "text": "Continue the task or respond to the channel."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
    ]


@pytest.mark.asyncio
async def test_agent_normalizes_multimodal_prompt_for_responses_api() -> None:
    agent = _make_agent()
    agent.settings.api_format = "responses"
    fork_capture = _ForkCapture()
    fake_tapes = _ContinueThenTextTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    result = await agent.run(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert result == "done"
    assert fake_tapes.prompts[1] == [
        {"type": "input_text", "text": "Continue the task or respond to the channel."},
        {"type": "input_image", "image_url": "data:image/png;base64,cG5n", "detail": "auto"},
    ]


def test_continue_prompt_keeps_inbound_media_parts_across_steps() -> None:
    state = {
        "_inbound_media_parts": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,aW5ib3VuZA=="}}],
        "_tool_media_parts": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,dG9vbA=="}}],
    }

    prompt = _continue_prompt(state)

    assert prompt == [
        {"type": "text", "text": "Continue the task or respond to the channel."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aW5ib3VuZA=="}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,dG9vbA=="}},
    ]
    assert "_inbound_media_parts" in state
    assert "_tool_media_parts" not in state


def test_event_prompt_redacts_image_parts() -> None:
    assert _event_prompt(
        [
            {"type": "text", "text": "Continue the task or respond to the channel."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
        ]
    ) == [
        {"type": "text", "text": "Continue the task or respond to the channel."},
        {"type": "image_url", "redacted": True},
    ]


def test_prompt_for_tape_replaces_image_parts_with_summary_text() -> None:
    prompt = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
    ]

    assert _prompt_for_tape(prompt) == "describe this\n\n[1 image omitted from tape history]"


@pytest.mark.asyncio
async def test_run_tools_without_persisting_prompt_keeps_new_messages_empty() -> None:
    @dataclass(frozen=True)
    class _Prepared:
        payload: list[dict[str, Any]]
        new_messages: list[dict[str, Any]]
        toolset: Any
        tape: str | None
        should_update: bool
        context_error: Any
        run_id: str
        system_prompt: str | None
        context: Any

    class _Client:
        async def _prepare_request_async(self, **kwargs: Any) -> _Prepared:
            return _Prepared(
                payload=kwargs["messages"],
                new_messages=[],
                toolset=SimpleNamespace(payload=[]),
                tape=kwargs["tape"],
                should_update=False,
                context_error=None,
                run_id="run-1",
                system_prompt=kwargs["system_prompt"],
                context=kwargs["context"],
            )

        async def _execute_async(self, prepared: _Prepared, **kwargs: Any) -> ToolAutoResult:
            assert prepared.should_update is True
            assert prepared.new_messages == []
            return ToolAutoResult.text_result("done")

        async def _handle_tools_auto_response_async(self, prepared: _Prepared, *args: Any, **kwargs: Any) -> Any:
            del prepared, args, kwargs
            raise AssertionError("unexpected on_response call")

    tape = SimpleNamespace(
        name="test-tape",
        context=SimpleNamespace(state={}),
        _client=_Client(),
        read_messages_async=AsyncMock(return_value=[{"role": "user", "content": "hello"}]),
    )

    result = await _run_tools_without_persisting_prompt(
        tape=tape,
        prompt=[{"type": "text", "text": "Continue the task or respond to the channel."}],
        system_prompt="system",
        tools=[],
        max_tokens=32,
        model=None,
        extra_options={},
    )

    assert result.kind == "text"
    assert result.text == "done"


@pytest.mark.asyncio
async def test_run_tools_with_transient_prompt_persists_redacted_prompt() -> None:
    @dataclass(frozen=True)
    class _Prepared:
        payload: list[dict[str, Any]]
        new_messages: list[dict[str, Any]]
        toolset: Any
        tape: str | None
        should_update: bool
        context_error: Any
        run_id: str
        system_prompt: str | None
        context: Any

    captured: dict[str, Any] = {}

    class _Client:
        async def _prepare_request_async(self, **kwargs: Any) -> _Prepared:
            captured["messages"] = kwargs["messages"]
            return _Prepared(
                payload=kwargs["messages"],
                new_messages=[],
                toolset=SimpleNamespace(payload=[]),
                tape=kwargs["tape"],
                should_update=False,
                context_error=None,
                run_id="run-1",
                system_prompt=kwargs["system_prompt"],
                context=kwargs["context"],
            )

        async def _execute_async(self, prepared: _Prepared, **kwargs: Any) -> ToolAutoResult:
            captured["prepared"] = prepared
            return ToolAutoResult.text_result("done")

        async def _handle_tools_auto_response_async(self, prepared: _Prepared, *args: Any, **kwargs: Any) -> Any:
            del prepared, args, kwargs
            raise AssertionError("unexpected on_response call")

    prompt = [
        {"type": "text", "text": "describe this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}},
    ]
    tape = SimpleNamespace(
        name="test-tape",
        context=SimpleNamespace(state={}),
        _client=_Client(),
        read_messages_async=AsyncMock(return_value=[{"role": "user", "content": "hello"}]),
    )

    result = await _run_tools_with_transient_prompt(
        tape=tape,
        prompt=prompt,
        persisted_prompt=_prompt_for_tape(prompt),
        system_prompt="system",
        tools=[],
        max_tokens=32,
        model=None,
        extra_options={},
    )

    assert result.kind == "text"
    assert result.text == "done"
    assert captured["messages"][-1] == {"role": "user", "content": prompt}
    assert captured["prepared"].new_messages == [
        {"role": "user", "content": "describe this\n\n[1 image omitted from tape history]"}
    ]
    assert captured["prepared"].system_prompt == "system"
