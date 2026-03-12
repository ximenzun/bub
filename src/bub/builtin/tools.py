from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import uuid
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, Field
from republic import ToolContext

from bub.builtin.shell_manager import shell_manager
from bub.commands import SlashCommandSpec
from bub.skills import discover_skills
from bub.tools import REGISTRY, tool

if TYPE_CHECKING:
    from bub.builtin.agent import Agent

type EntryKind = Literal["event", "anchor", "system", "message", "tool_call", "tool_result"]

DEFAULT_COMMAND_TIMEOUT_SECONDS = 30
DEFAULT_HEADERS = {"accept": "text/markdown"}
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10


def _get_agent(context: ToolContext) -> Agent:
    if "_runtime_agent" not in context.state:
        raise RuntimeError("no runtime agent found in tool context")
    return cast("Agent", context.state["_runtime_agent"])


class SearchInput(BaseModel):
    query: str = Field(..., description="The search query string.")
    limit: int = Field(20, description="Maximum number of search results to return.")
    start: str | None = Field(None, description="Optional start date to filter entries (ISO format).")
    end: str | None = Field(None, description="Optional end date to filter entries (ISO format).")
    kinds: list[EntryKind] = Field(
        default=["message", "tool_result"],
        description="Optional list of entry kinds to filter search results.",
    )


class SubAgentInput(BaseModel):
    prompt: str | list[dict[str, Any]] = Field(
        ...,
        description="The initial prompt for the sub-agent, either as a string or multimodal content parts.",
    )
    model: str | None = Field(None, description="Optional model override for the sub-agent.")
    session: str = Field(
        "temp",
        description="Session handling strategy: 'inherit', 'temp', or an explicit session id.",
    )
    allowed_tools: list[str] | None = Field(
        None,
        description="Optional allow-list of tools the sub-agent may use.",
    )
    allowed_skills: list[str] | None = Field(
        None,
        description="Optional allow-list of skills the sub-agent may use.",
    )


def _resolve_rg_binary() -> Path | None:
    candidate = shutil.which("rg")
    if candidate:
        return Path(candidate)

    home = Path.home()
    extra_candidates = [
        home / ".local" / "bin" / "rg",
        Path("/opt/homebrew/bin/rg"),
        Path("/usr/local/bin/rg"),
        Path("/usr/bin/rg"),
    ]
    for path in extra_candidates:
        if path.is_file():
            return path

    codex_vendor_root = home / ".local" / "share" / "mise" / "installs"
    for path in codex_vendor_root.glob(
        "node/*/lib/node_modules/@openai/codex/node_modules/@openai/codex-*/vendor/*/path/rg"
    ):
        if path.is_file():
            return path
    return None


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    rg_binary = _resolve_rg_binary()
    if rg_binary is None:
        return env
    current_path = env.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    rg_dir = str(rg_binary.parent)
    if rg_dir not in path_entries:
        env["PATH"] = os.pathsep.join([rg_dir, *path_entries]) if path_entries else rg_dir
    return env


def _is_search_no_match(cmd: str, returncode: int, stderr_text: str) -> bool:
    if returncode != 1 or stderr_text:
        return False
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    if not argv:
        return False
    executable = PurePath(argv[0]).name
    if executable in {"rg", "grep"}:
        return True
    return len(argv) > 1 and executable == "git" and argv[1] == "grep"


def _nested_wecom_longconn_send_guidance(cmd: str, context: ToolContext) -> str | None:
    inbound_channel = context.state.get("_inbound_channel")
    if inbound_channel != "wecom_longconn_bot":
        return None
    if "wecom_longconn_send.py" not in cmd:
        return None
    return (
        "nested wecom_longconn_send.py is disabled while handling wecom_longconn_bot inbound messages; "
        "use Bub's native outbound routing and return the final text reply directly instead"
    )


@tool(context=True)
async def bash(
    cmd: str,
    cwd: str | None = None,
    timeout_seconds: int = DEFAULT_COMMAND_TIMEOUT_SECONDS,
    background: bool = False,
    *,
    context: ToolContext,
) -> str:
    """Run a shell command. Use background=true to keep it running and fetch output later."""
    if guidance := _nested_wecom_longconn_send_guidance(cmd, context):
        return guidance
    workspace = context.state.get("_runtime_workspace")
    shell = await shell_manager.start(cmd=cmd, cwd=cwd or workspace, env=_subprocess_env())
    if background:
        return f"started: {shell.shell_id}"
    try:
        async with asyncio.timeout(timeout_seconds):
            shell = await shell_manager.wait_closed(shell.shell_id)
    except TimeoutError as exc:
        await shell_manager.terminate(shell.shell_id, timeout_seconds=min(float(timeout_seconds), 5.0))
        raise TimeoutError(f"command timed out after {timeout_seconds}s: {cmd}") from exc
    stdout_text = shell.stdout.strip()
    stderr_text = shell.stderr.strip()
    if _is_search_no_match(cmd, shell.returncode or 0, stderr_text):
        return "(no matches)"
    if shell.returncode != 0:
        message = stderr_text or stdout_text or f"exit={shell.returncode}"
        raise RuntimeError(f"exit={shell.returncode}: {message}")
    return stdout_text or "(no output)"


@tool(name="bash.output")
async def bash_output(shell_id: str, offset: int = 0, limit: int | None = None) -> str:
    """Read buffered output from a background shell, with optional offset and limit."""
    shell = shell_manager.get(shell_id)
    if shell.returncode is not None:
        await shell_manager.wait_closed(shell_id)
    output = shell.output
    start = max(0, min(offset, len(output)))
    end = len(output) if limit is None else min(len(output), start + max(0, limit))
    chunk = output[start:end].rstrip()
    exit_code = "null" if shell.returncode is None else str(shell.returncode)
    body = chunk or "(no output)"
    return f"id: {shell.shell_id}\nstatus: {shell.status}\nexit_code: {exit_code}\nnext_offset: {end}\noutput:\n{body}"


@tool(name="bash.kill")
async def kill_bash(shell_id: str) -> str:
    """Terminate a background shell process."""
    shell = shell_manager.get(shell_id)
    if shell.returncode is None:
        shell = await shell_manager.terminate(shell_id)
    else:
        await shell_manager.wait_closed(shell_id)
    return f"id: {shell.shell_id}\nstatus: {shell.status}\nexit_code: {shell.returncode}"


@tool(context=True, name="fs.read")
def fs_read(path: str, offset: int = 0, limit: int | None = None, *, context: ToolContext) -> str:
    """Read a text file and return its content. Supports optional pagination with offset and limit."""
    resolved_path = _resolve_path(context, path)
    text = resolved_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = max(0, min(offset, len(lines)))
    end = len(lines) if limit is None else min(len(lines), start + max(0, limit))
    return "\n".join(lines[start:end])


@tool(context=True, name="fs.write")
def fs_write(path: str, content: str, *, context: ToolContext) -> str:
    """Write content to a text file."""
    resolved_path = _resolve_path(context, path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(content, encoding="utf-8")
    return f"wrote: {resolved_path}"


@tool(context=True, name="fs.edit")
def fs_edit(path: str, old: str, new: str, start: int = 0, *, context: ToolContext) -> str:
    """Edit a text file by replacing old text with new text. You can specify the line number to start searching for the old text."""
    resolved_path = _resolve_path(context, path)
    text = resolved_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    prev, to_replace = "\n".join(lines[:start]), "\n".join(lines[start:])
    if old not in to_replace:
        raise ValueError(f"'{old}' not found in {resolved_path} from line {start}")
    replaced = to_replace.replace(old, new)
    if prev:
        replaced = prev + "\n" + replaced
    resolved_path.write_text(replaced, encoding="utf-8")
    return f"edited: {resolved_path}"


@tool(context=True, name="skill")
def skill_describe(name: str, *, context: ToolContext) -> str:
    """Load the skill content by name. Return the location and skill content."""
    from bub.utils import workspace_from_state

    allowed_skills = context.state.get("allowed_skills")
    if allowed_skills is not None and name.casefold() not in allowed_skills:
        return f"(skill '{name}' is not allowed in this context)"

    workspace = workspace_from_state(context.state)
    skill_index = {skill.name.casefold(): skill for skill in discover_skills(workspace)}
    if name.casefold() not in skill_index:
        return "(no such skill)"
    skill = skill_index[name.casefold()]
    return f"Location: {skill.location}\n---\n{skill.body() or '(no content)'}"


@tool(context=True, name="tape.info")
async def tape_info(context: ToolContext) -> str:
    """Get information about the current tape, such as number of entries and anchors."""
    agent = _get_agent(context)
    info = await agent.tapes.info(context.tape or "")
    return (
        f"name: {info.name}\n"
        f"entries: {info.entries}\n"
        f"anchors: {info.anchors}\n"
        f"last_anchor: {info.last_anchor}\n"
        f"entries_since_last_anchor: {info.entries_since_last_anchor}\n"
        f"last_token_usage: {info.last_token_usage}"
    )


@tool(context=True, name="tape.search", model=SearchInput)
async def tape_search(param: SearchInput, *, context: ToolContext) -> str:
    """Search for entries in the current tape that match the query. Returns a list of matching entries."""
    import yaml

    agent = _get_agent(context)
    entries = await agent.tapes.search(
        context.tape or "",
        query=param.query,
        limit=param.limit,
        start=param.start,
        end=param.end,
        kinds=tuple(param.kinds),
    )
    if not entries:
        return "(no matches)"
    return yaml.safe_dump(
        [{"date": entry.date, "kind": entry.kind, "data": entry.payload} for entry in entries],
        sort_keys=False,
        allow_unicode=True,
    )


@tool(context=True, name="tape.reset")
async def tape_reset(archive: bool = False, *, context: ToolContext) -> str:
    """Reset the current tape, optionally archiving it."""
    agent = _get_agent(context)
    result = await agent.tapes.reset(context.tape or "", archive=archive)
    return result


@tool(context=True, name="tape.handoff")
async def tape_handoff(name: str = "handoff", summary: str = "", *, context: ToolContext) -> str:
    """Add a handoff anchor to the current tape."""
    agent = _get_agent(context)
    await agent.tapes.handoff(context.tape or "", name=name, state={"summary": summary})
    return f"anchor added: {name}"


@tool(context=True, name="tape.anchors")
async def tape_anchors(*, context: ToolContext) -> str:
    """List anchors in the current tape."""
    agent = _get_agent(context)
    anchors = await agent.tapes.anchors(context.tape or "")
    if not anchors:
        return "(no anchors)"
    return "\n".join(f"- {anchor.name}" for anchor in anchors)


@tool(name="subagent", context=True, model=SubAgentInput)
async def run_subagent(param: SubAgentInput, *, context: ToolContext) -> str:
    """Run a task through a constrained sub-agent."""
    agent = _get_agent(context)
    session_id = str(context.state.get("session_id", "temp/unknown"))
    if param.session == "inherit":
        subagent_session = session_id
    elif param.session == "temp":
        subagent_session = f"temp/{uuid.uuid4().hex[:8]}"
    else:
        subagent_session = param.session
    if param.allowed_tools is None:
        allowed_tools: list[str] | None = sorted(name for name in REGISTRY if name != "subagent")
    else:
        allowed_tools = [name for name in param.allowed_tools if name != "subagent"]
    state = {**context.state, "session_id": subagent_session}
    return await agent.run(
        session_id=subagent_session,
        prompt=param.prompt,
        state=state,
        model=param.model,
        allowed_tools=allowed_tools,
        allowed_skills=param.allowed_skills,
    )


@tool(name="web.fetch")
async def web_fetch(url: str, headers: dict | None = None, timeout: int | None = None) -> str:
    """Fetch(GET) the content of a web page, returning markdown if possible."""
    import aiohttp

    headers = {**DEFAULT_HEADERS, **(headers or {})}
    timeout = timeout or DEFAULT_REQUEST_TIMEOUT_SECONDS

    async with (
        aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as session,
        session.get(url) as response,
    ):
        response.raise_for_status()
        return await response.text()


@tool(name="help")
def show_help() -> str:
    """Show a help message."""
    return (
        "Commands use ',' at line start.\n"
        "Known internal commands:\n"
        "  ,help\n"
        "  ,commands\n"
        "  ,skill name=foo\n"
        "  ,tape.info\n"
        "  ,tape.search query=error\n"
        "  ,tape.handoff name=phase-1 summary='done'\n"
        "  ,tape.anchors\n"
        "  ,subagent prompt='summarize README' session=temp\n"
        "  ,fs.read path=README.md\n"
        "  ,fs.write path=tmp.txt content='hello'\n"
        "  ,fs.edit path=tmp.txt old=hello new=world\n"
        "  ,bash cmd='sleep 5' background=true\n"
        "  ,bash.output shell_id=bash-12345678\n"
        "  ,bash.kill shell_id=bash-12345678\n"
        "Any unknown command after ',' is executed as shell via bash."
    )


@tool(context=True, name="commands")
def show_commands(topic: str = "", *, context: ToolContext) -> str:
    """Show available chat slash commands and their usage."""
    agent = _get_agent(context)
    commands = agent.framework.get_slash_commands()
    topic_name = topic.strip().casefold().lstrip("/")
    if topic_name:
        for command in commands:
            if command.topic_key == topic_name or command.name.lstrip("/").casefold() == topic_name:
                return _render_slash_command_detail(command)
        return f"(no such slash command: {topic_name})\n\n{_render_slash_command_index(commands)}"
    return _render_slash_command_index(commands)


def _render_slash_command_index(commands: list[SlashCommandSpec]) -> str:
    if not commands:
        return "(no slash commands registered)"
    lines = [
        "Available slash commands:",
        "",
        "Use `/commands <topic>` or send `/<topic>` to view command-specific help.",
    ]
    for command in commands:
        lines.append(f"- {command.name}: {command.summary}")
    examples = _collect_slash_examples(commands)
    if examples:
        lines.append("")
        lines.append("Quick examples:")
        lines.extend(f"- {example}" for example in examples)
    return "\n".join(lines)


def _render_slash_command_detail(command: SlashCommandSpec) -> str:
    lines = [f"{command.name}: {command.summary}"]
    if command.usage:
        lines.append("")
        lines.append("Usage:")
        lines.extend(f"- {item}" for item in command.usage)
    if command.examples:
        lines.append("")
        lines.append("Examples:")
        lines.extend(f"- {item}" for item in command.examples)
    lines.append("")
    lines.append("Send `/commands` to see all available commands.")
    return "\n".join(lines)


def _collect_slash_examples(commands: list[SlashCommandSpec], limit: int = 4) -> list[str]:
    examples: list[str] = []
    for command in commands:
        if command.examples:
            examples.extend(command.examples)
        elif command.usage:
            examples.append(command.usage[0])
        if len(examples) >= limit:
            break
    return examples[:limit]


def _resolve_path(context: ToolContext, raw_path: str) -> Path:
    workspace = context.state.get("_runtime_workspace")
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    if workspace is None:
        raise ValueError(f"relative path '{raw_path}' is not allowed without a workspace")
    if not isinstance(workspace, str | Path):
        raise TypeError("runtime workspace must be a filesystem path")
    workspace_path = Path(workspace)
    return (workspace_path / path).resolve()
