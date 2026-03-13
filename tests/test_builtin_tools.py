from __future__ import annotations

import asyncio
import shlex
import sys
from types import SimpleNamespace

import pytest
from republic import ToolContext

from bub.builtin.tools import bash, bash_output, kill_bash, show_commands
from bub.commands import SlashCommandSpec


def _tool_context(tmp_path) -> ToolContext:
    return ToolContext(tape="test-tape", run_id="test-run", state={"_runtime_workspace": str(tmp_path)})


def _python_shell(code: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.mark.asyncio
async def test_bash_returns_stdout_for_foreground_command(tmp_path) -> None:
    result = await bash.run(cmd=_python_shell("print('hello')"), context=_tool_context(tmp_path))

    assert result == "hello"


@pytest.mark.asyncio
async def test_background_bash_exposes_output_via_bash_output(tmp_path) -> None:
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
async def test_kill_bash_terminates_background_process(tmp_path) -> None:
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
async def test_kill_bash_returns_status_when_process_already_finished(tmp_path) -> None:
    started = await bash.run(
        cmd=_python_shell("print('done')"),
        background=True,
        context=_tool_context(tmp_path),
    )
    shell_id = started.removeprefix("started: ").strip()

    await asyncio.sleep(0.1)
    result = await kill_bash.run(shell_id=shell_id)

    assert result == f"id: {shell_id}\nstatus: exited\nexit_code: 0"


@pytest.mark.asyncio
async def test_show_commands_renders_registered_slash_commands(tmp_path) -> None:
    framework = SimpleNamespace(
        get_slash_commands=lambda: [
            SlashCommandSpec(name="/repo", summary="Repo management", usage=("/repo list",), examples=("/repo list",))
        ]
    )
    agent = SimpleNamespace(framework=framework)
    context = ToolContext(
        tape="test-tape",
        run_id="test-run",
        state={"_runtime_workspace": str(tmp_path), "_runtime_agent": agent},
    )

    result = await show_commands.run(context=context)

    assert "Available slash commands:" in result
    assert "- /repo: Repo management" in result


@pytest.mark.asyncio
async def test_show_commands_renders_topic_detail(tmp_path) -> None:
    framework = SimpleNamespace(
        get_slash_commands=lambda: [
            SlashCommandSpec(
                name="/repo",
                summary="Repo management",
                usage=("/repo list", "/repo bind demo"),
                examples=("/repo list",),
            )
        ]
    )
    agent = SimpleNamespace(framework=framework)
    context = ToolContext(
        tape="test-tape",
        run_id="test-run",
        state={"_runtime_workspace": str(tmp_path), "_runtime_agent": agent},
    )

    result = await show_commands.run(topic="repo", context=context)

    assert result.startswith("/repo: Repo management")
    assert "Usage:" in result
    assert "Send `/commands` to see all available commands." in result
