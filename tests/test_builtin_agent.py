from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from republic import ToolAutoResult

from bub.builtin.agent import Agent
from bub.builtin.settings import AgentSettings


def _make_agent() -> Agent:
    framework = MagicMock()
    framework.get_tape_store.return_value = None
    framework.get_system_prompt.return_value = ""

    with patch.object(Agent, "__init__", lambda self, fw: None):
        agent = Agent.__new__(Agent)

    agent.settings = AgentSettings(model="test:model", api_key="k", api_base="b")
    agent.framework = framework
    return agent


class _ForkCapture:
    def __init__(self) -> None:
        self.merge_back_values: list[bool] = []

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        self.merge_back_values.append(merge_back)
        yield


class _FakeTapeService:
    def __init__(self, fork_capture: _ForkCapture) -> None:
        self._fork = fork_capture
        self.run_tools_kwargs: dict[str, Any] | None = None

    def session_tape(self, session_id: str, workspace: Any) -> MagicMock:
        tape = MagicMock()
        tape.name = "test-tape"
        tape.context.state = {}

        async def fake_run_tools_async(**kwargs: Any) -> ToolAutoResult:
            self.run_tools_kwargs = kwargs
            return ToolAutoResult(kind="text", text="done", tool_calls=[], tool_results=[], error=None)

        tape.run_tools_async = fake_run_tools_async
        return tape

    async def ensure_bootstrap_anchor(self, tape_name: str) -> None:
        return None

    async def append_event(self, tape_name: str, name: str, payload: dict[str, Any]) -> None:
        return None

    @contextlib.asynccontextmanager
    async def fork_tape(self, tape_name: str, merge_back: bool = True) -> AsyncGenerator[None, None]:
        async with self._fork.fork_tape(tape_name, merge_back=merge_back):
            yield


@pytest.mark.asyncio
async def test_agent_run_regular_session_merges_back() -> None:
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    await agent.run(session_id="user/session1", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [True]


@pytest.mark.asyncio
async def test_agent_run_temp_session_does_not_merge_back() -> None:
    agent = _make_agent()
    fork_capture = _ForkCapture()
    agent.tapes = _FakeTapeService(fork_capture)  # type: ignore[assignment]

    await agent.run(session_id="temp/abc123", prompt="hello", state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fork_capture.merge_back_values == [False]


@pytest.mark.asyncio
async def test_agent_run_passes_model_to_llm() -> None:
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    await agent.run(session_id="user/s1", prompt="hello", state={"_runtime_workspace": "/tmp"}, model="openai:gpt-4o")  # noqa: S108

    assert fake_tapes.run_tools_kwargs is not None
    assert fake_tapes.run_tools_kwargs["model"] == "openai:gpt-4o"
    assert fake_tapes.run_tools_kwargs["prompt"] == "hello"


@pytest.mark.asyncio
async def test_agent_run_multimodal_prompt_uses_messages() -> None:
    agent = _make_agent()
    fork_capture = _ForkCapture()
    fake_tapes = _FakeTapeService(fork_capture)
    agent.tapes = fake_tapes  # type: ignore[assignment]

    prompt = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    await agent.run(session_id="user/s1", prompt=prompt, state={"_runtime_workspace": "/tmp"})  # noqa: S108

    assert fake_tapes.run_tools_kwargs is not None
    assert "prompt" not in fake_tapes.run_tools_kwargs
    assert fake_tapes.run_tools_kwargs["messages"] == [{"role": "user", "content": prompt}]

