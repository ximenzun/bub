from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from bub.builtin.tools import run_subagent


class FakeContext:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.tape = None


class FakeAgent:
    def __init__(self) -> None:
        self.run = AsyncMock(return_value="agent result")


@pytest.mark.asyncio
async def test_subagent_inherit_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    result = await run_subagent.run(prompt="do something", session="inherit", context=ctx)

    assert result == "agent result"
    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["session_id"] == "user/abc"
    assert call_kwargs["prompt"] == "do something"
    assert call_kwargs["model"] is None


@pytest.mark.asyncio
async def test_subagent_temp_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", session="temp", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["session_id"].startswith("temp/")
    assert call_kwargs["session_id"] != "user/abc"


@pytest.mark.asyncio
async def test_subagent_custom_session() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(prompt="task", session="custom/session-1", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["session_id"] == "custom/session-1"


@pytest.mark.asyncio
async def test_subagent_passes_model_and_allow_lists() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc"})

    await run_subagent.run(
        prompt="task",
        model="openai:gpt-4o",
        allowed_tools=["bash"],
        allowed_skills=["wecom"],
        context=ctx,
    )

    call_kwargs = agent.run.call_args.kwargs
    assert call_kwargs["model"] == "openai:gpt-4o"
    assert call_kwargs["allowed_tools"] == ["bash"]
    assert call_kwargs["allowed_skills"] == ["wecom"]


@pytest.mark.asyncio
async def test_subagent_state_includes_subagent_session_id() -> None:
    agent = FakeAgent()
    ctx = FakeContext({"_runtime_agent": agent, "session_id": "user/abc", "extra": "val"})

    await run_subagent.run(prompt="task", session="temp", context=ctx)

    call_kwargs = agent.run.call_args.kwargs
    state = call_kwargs["state"]
    assert state["session_id"] == call_kwargs["session_id"]
    assert state["extra"] == "val"

