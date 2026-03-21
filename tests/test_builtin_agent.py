from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from bub.builtin.agent import Agent
from bub.framework import BubFramework
from bub.tools import REGISTRY, tool


class _FakeTapeService:
    def __init__(self, tape) -> None:
        self._tape = tape

    def session_tape(self, session_id: str, workspace: Path):
        return self._tape

    @asynccontextmanager
    async def fork_tape(self, name: str, merge_back: bool = True):
        yield

    async def ensure_bootstrap_anchor(self, name: str) -> None:
        return None

    async def hydrate_context(self, tape, runtime_state: dict[str, object]) -> None:
        tape.context.state = dict(runtime_state)

    async def append_event(self, tape_name: str, kind: str, payload: dict[str, object]) -> None:
        return None


@pytest.mark.asyncio
async def test_agent_run_command_syncs_tape_state_back_to_framework_state() -> None:
    command_name = "test.state.sync"

    @tool(name=command_name, context=True)
    def _state_sync_tool(*, context) -> str:
        context.state["_suppress_default_outbound"] = True
        return ""

    framework = BubFramework()
    agent = Agent(framework)
    tape = SimpleNamespace(name="test-tape", context=SimpleNamespace(state={}))
    agent.__dict__["tapes"] = _FakeTapeService(tape)
    state: dict[str, object] = {"_runtime_workspace": str(Path.cwd())}

    try:
        result = await agent.run(session_id="cli:room", prompt=f",{command_name}", state=state)
    finally:
        REGISTRY.pop(command_name, None)

    assert result == ""
    assert state["_suppress_default_outbound"] is True
