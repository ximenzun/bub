from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest
from republic import ToolContext

from bub.builtin.tools import bash, bash_output, kill_bash, skill_describe


def _tool_context(tmp_path: Path, **state: object) -> ToolContext:
    return ToolContext(tape="test-tape", run_id="test-run", state={"_runtime_workspace": str(tmp_path), **state})


def _python_shell(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.mark.asyncio
async def test_bash_returns_stdout_for_foreground_command(tmp_path: Path) -> None:
    result = await bash.run(cmd=_python_shell("print('hello')"), context=_tool_context(tmp_path))

    assert result == "hello"


@pytest.mark.asyncio
async def test_background_bash_exposes_output_via_bash_output(tmp_path: Path) -> None:
    command = _python_shell(
        "import sys, time; print('start'); sys.stdout.flush(); time.sleep(0.2); print('done'); sys.stdout.flush()"
    )

    started = await bash.run(cmd=command, background=True, context=_tool_context(tmp_path))
    shell_id = started.removeprefix("started: ").strip()

    await asyncio.sleep(0.35)
    output = await bash_output.run(shell_id=shell_id)

    assert output.startswith(f"id: {shell_id}\nstatus: exited\n")
    assert "exit_code: 0" in output
    assert "start" in output
    assert "done" in output


@pytest.mark.asyncio
async def test_kill_bash_terminates_background_process(tmp_path: Path) -> None:
    started = await bash.run(
        cmd=_python_shell("import time; time.sleep(10)"),
        background=True,
        context=_tool_context(tmp_path),
    )
    shell_id = started.removeprefix("started: ").strip()

    killed = await kill_bash.run(shell_id=shell_id)
    output = await bash_output.run(shell_id=shell_id)

    assert killed.startswith(f"id: {shell_id}\nstatus: exited\nexit_code: ")
    assert "exit_code: null" not in killed
    assert output.startswith(f"id: {shell_id}\nstatus: exited\n")


@pytest.mark.asyncio
async def test_skill_describe_respects_allowed_skills(tmp_path: Path) -> None:
    allowed_dir = tmp_path / ".agents" / "skills" / "allowed-skill"
    blocked_dir = tmp_path / ".agents" / "skills" / "blocked-skill"
    allowed_dir.mkdir(parents=True)
    blocked_dir.mkdir(parents=True)
    (allowed_dir / "SKILL.md").write_text(
        "---\nname: allowed-skill\ndescription: allowed\n---\nAllowed body",
        encoding="utf-8",
    )
    (blocked_dir / "SKILL.md").write_text(
        "---\nname: blocked-skill\ndescription: blocked\n---\nBlocked body",
        encoding="utf-8",
    )

    context = _tool_context(tmp_path, allowed_skills=["allowed-skill"])

    assert "Allowed body" in await skill_describe.run(name="allowed-skill", context=context)
    assert (
        await skill_describe.run(name="blocked-skill", context=context)
        == "(skill 'blocked-skill' is not allowed in this context)"
    )
