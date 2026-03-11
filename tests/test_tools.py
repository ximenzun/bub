from __future__ import annotations

import os
from typing import Any, cast

import pytest
from loguru import logger
from pydantic import BaseModel
from republic import Tool, ToolContext

from bub.tools import REGISTRY, model_tools, render_tools_prompt, tool


class EchoInput(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_tool_decorator_registers_tool_and_preserves_metadata() -> None:
    tool_name = "tests.sync_tool"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="Sync test tool", model=EchoInput)
    def sync_tool(payload: EchoInput) -> str:
        return payload.value.upper()

    assert sync_tool.name == tool_name
    assert sync_tool.description == "Sync test tool"
    assert REGISTRY[tool_name] is sync_tool
    assert await sync_tool.run(value="hello") == "HELLO"


@pytest.mark.asyncio
async def test_tool_wrapper_logs_and_omits_context_from_log_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_name = "tests.async_tool"
    REGISTRY.pop(tool_name, None)
    messages: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        messages.append(message.format(*args, **kwargs))

    monkeypatch.setattr(logger, "info", record)

    @tool(name=tool_name, description="Async test tool", context=True)
    async def async_tool(value: str, context: object) -> str:
        return f"{value}:{context}"

    result = await async_tool.run("hello", context="ctx")

    assert result == "hello:ctx"
    assert REGISTRY[tool_name] is async_tool
    assert len(messages) == 2
    assert messages[0] == 'tool.call.start name=tests.async_tool { "hello" }'
    assert messages[1].startswith("tool.call.success name=tests.async_tool elapsed_time=")


@pytest.mark.asyncio
async def test_tool_wrapper_logs_failures_before_reraising(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_name = "tests.failing_tool"
    REGISTRY.pop(tool_name, None)
    errors: list[str] = []

    def record_exception(message: str, *args: Any, **kwargs: Any) -> None:
        errors.append(message.format(*args, **kwargs))

    monkeypatch.setattr(logger, "exception", record_exception)

    @tool(name=tool_name)
    def failing_tool() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await failing_tool.run()

    assert len(errors) == 1
    assert errors[0].startswith("tool.call.error name=tests.failing_tool elapsed_time=")


def test_model_tools_rewrites_dotted_names_without_mutating_original() -> None:
    tool_name = "tests.rename_me"
    REGISTRY.pop(tool_name, None)

    @tool(name=tool_name, description="rename")
    def rename_me() -> str:
        return "ok"

    rewritten = model_tools([rename_me])

    assert [item.name for item in rewritten] == ["tests_rename_me"]
    assert rename_me.name == tool_name


def test_render_tools_prompt_renders_available_tools_block() -> None:
    first_name = "tests.prompt_one"
    second_name = "tests.prompt_two"
    REGISTRY.pop(first_name, None)
    REGISTRY.pop(second_name, None)

    @tool(name=first_name, description="First tool")
    def prompt_one() -> str:
        return "one"

    @tool(name=second_name)
    def prompt_two() -> str:
        return "two"

    rendered = render_tools_prompt([prompt_one, prompt_two])

    assert rendered == "<available_tools>\n- tests_prompt_one: First tool\n- tests_prompt_two\n</available_tools>"


def test_render_tools_prompt_returns_empty_string_for_empty_input() -> None:
    assert render_tools_prompt([]) == ""


def test_subprocess_env_prepends_rg_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    from bub.builtin import tools as builtin_tools

    rg_binary = builtin_tools.Path.home() / "test-tools" / "rg"
    monkeypatch.setattr(builtin_tools, "_resolve_rg_binary", lambda: rg_binary)
    monkeypatch.setattr(builtin_tools.os, "environ", {"PATH": "/usr/bin:/bin"})

    env = builtin_tools._subprocess_env()

    assert env["PATH"] == os.pathsep.join([str(rg_binary.parent), "/usr/bin", "/bin"])


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ('rg -n "needle" src', True),
        ("grep -R needle src", True),
        ("git grep needle", True),
        ("python script.py", False),
    ],
)
def test_is_search_no_match_recognizes_search_commands(cmd: str, expected: bool) -> None:
    from bub.builtin import tools as builtin_tools

    assert builtin_tools._is_search_no_match(cmd, 1, "") is expected
    assert builtin_tools._is_search_no_match(cmd, 2, "") is False
    assert builtin_tools._is_search_no_match(cmd, 1, "boom") is False


@pytest.mark.asyncio
async def test_builtin_bash_cleans_up_subprocess_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from bub.builtin import tools as builtin_tools

    calls: list[tuple[object, float, bool]] = []

    class FakeProcess:
        returncode = None

        async def communicate(self):
            return b"", b""

    class FakeTimeout:
        async def __aenter__(self):
            return None

        async def __aexit__(self, exc_type, exc, tb):
            raise TimeoutError

    async def fake_create_subprocess_shell(*args, **kwargs):
        return FakeProcess()

    async def fake_terminate_process(process, *, timeout_seconds: float, kill_process_group: bool = False) -> bool:
        calls.append((process, timeout_seconds, kill_process_group))
        return False

    monkeypatch.setattr(builtin_tools.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    monkeypatch.setattr(builtin_tools, "terminate_process", fake_terminate_process)
    monkeypatch.setattr(builtin_tools.asyncio, "timeout", lambda _seconds: FakeTimeout())
    monkeypatch.setattr(builtin_tools, "_subprocess_env", lambda: {})

    with pytest.raises(TimeoutError, match=r"command timed out after 1s: sleep 999"):
        await cast(Tool, builtin_tools.bash).run(
            cmd="sleep 999",
            timeout_seconds=1,
            context=ToolContext(tape="t", run_id="r", state={"_runtime_workspace": "/tmp"}),  # noqa: S108
        )

    assert len(calls) == 1
    assert calls[0][1] == 1.0
    assert calls[0][2] is (builtin_tools.sys.platform != "win32")


@pytest.mark.asyncio
async def test_builtin_bash_returns_no_matches_for_rg_exit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from bub.builtin import tools as builtin_tools

    class FakeProcess:
        returncode = 1

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_shell(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(builtin_tools.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    monkeypatch.setattr(builtin_tools, "_subprocess_env", lambda: {})

    result = await cast(Tool, builtin_tools.bash).run(
        cmd='rg -n "missing" src',
        timeout_seconds=1,
        context=ToolContext(tape="t", run_id="r", state={"_runtime_workspace": "/tmp"}),  # noqa: S108
    )

    assert result == "(no matches)"
