"""Republic-driven runtime engine to process prompts."""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
import time
from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import cached_property, partial
from pathlib import Path
from typing import Any

from loguru import logger
from republic import LLM, AsyncTapeStore, ToolAutoResult, ToolContext
from republic.tape import InMemoryTapeStore, Tape

from bub.builtin.context import default_tape_context
from bub.builtin.resource_refs import RESOURCE_REFS_KEY, coerce_resource_refs
from bub.builtin.settings import AgentSettings
from bub.builtin.store import ForkTapeStore
from bub.builtin.tape import TapeService
from bub.framework import BubFramework
from bub.skills import discover_skills, render_skills_prompt
from bub.tools import REGISTRY, model_tools, render_tools_prompt
from bub.types import State
from bub.utils import workspace_from_state

CONTINUE_PROMPT = "Continue the task or respond to the channel."
DEFAULT_BUB_HEADERS = {"HTTP-Referer": "https://bub.build/", "X-Title": "Bub"}
HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
INBOUND_MESSAGE_ID_KEY = "_bub_inbound_message_id"


class Agent:
    """Agent that processes prompts using hooks and tools. Backed by republic."""

    def __init__(self, framework: BubFramework) -> None:
        self.settings = _load_runtime_settings()
        self.framework = framework

    @cached_property
    def tapes(self) -> TapeService:
        tape_store = self.framework.get_tape_store()
        if tape_store is None:
            tape_store = InMemoryTapeStore()
        tape_store = ForkTapeStore(tape_store)
        llm = _build_llm(self.settings, tape_store)
        return TapeService(llm, self.settings.home / "tapes", tape_store)

    async def run(
        self,
        *,
        session_id: str,
        prompt: str | list[dict],
        state: State,
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> str:
        if not prompt:
            return "error: empty prompt"
        tape = self.tapes.session_tape(session_id, workspace_from_state(state))
        merge_back = not session_id.startswith("temp/")
        async with self.tapes.fork_tape(tape.name, merge_back=merge_back):
            await self.tapes.ensure_bootstrap_anchor(tape.name)
            await self.tapes.hydrate_context(tape, runtime_state=state)
            if isinstance(prompt, str) and prompt.strip().startswith(","):
                result = await self._run_command(tape=tape, line=prompt.strip())
                state.update(tape.context.state)
                return result
            result = await self._agent_loop(
                tape=tape,
                prompt=prompt,
                model=model,
                allowed_skills=allowed_skills,
                allowed_tools=allowed_tools,
            )
            state.update(tape.context.state)
            return result

    async def _run_command(self, tape: Tape, *, line: str) -> str:
        line = line[1:].strip()
        if not line:
            raise ValueError("empty command")

        name, arg_tokens = _parse_internal_command(line)
        start = time.monotonic()
        context = ToolContext(tape=tape.name, run_id="run_command", state=tape.context.state)
        output = ""
        status = "ok"
        try:
            if name not in REGISTRY:
                output = await REGISTRY["bash"].run(context=context, cmd=line)
            else:
                args = _parse_args(arg_tokens)
                if REGISTRY[name].context:
                    args.kwargs["context"] = context
                output = REGISTRY[name].run(*args.positional, **args.kwargs)
                if inspect.isawaitable(output):
                    output = await output
        except Exception as exc:
            status = "error"
            output = f"{exc!s}"
            raise
        else:
            return output if isinstance(output, str) else str(output)
        finally:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            output_text = output if isinstance(output, str) else str(output)

            event_payload = {
                "raw": line,
                "name": name,
                "status": status,
                "elapsed_ms": elapsed_ms,
                "output": output_text,
                "date": datetime.now(UTC).isoformat(),
            }
            await self.tapes.append_event(tape.name, "command", event_payload)

    async def _agent_loop(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = None,
        allowed_skills: Collection[str] | None = None,
        allowed_tools: Collection[str] | None = None,
    ) -> str:
        next_prompt: str | list[dict] = prompt
        display_model = model or self.settings.model
        for step in range(1, self.settings.max_steps + 1):
            start = time.monotonic()
            logger.info("loop.step step={} tape={} model={}", step, tape.name, display_model)
            await self.tapes.append_event(tape.name, "loop.step.start", {"step": step, "prompt": _event_prompt(next_prompt)})
            try:
                output = await self._run_tools_once(
                    tape=tape,
                    prompt=next_prompt,
                    model=model,
                    allowed_skills=allowed_skills,
                    allowed_tools=allowed_tools,
                )
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "error",
                        "error": f"{exc!s}",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                raise

            outcome = _resolve_tool_auto_result(output)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if outcome.kind == "text":
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "ok",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                return outcome.text
            if outcome.kind == "continue":
                next_prompt = _continue_prompt(tape.context.state)
                await self.tapes.append_event(
                    tape.name,
                    "loop.step",
                    {
                        "step": step,
                        "elapsed_ms": elapsed_ms,
                        "status": "continue",
                        "date": datetime.now(UTC).isoformat(),
                    },
                )
                continue
            await self.tapes.append_event(
                tape.name,
                "loop.step",
                {
                    "step": step,
                    "elapsed_ms": elapsed_ms,
                    "status": "error",
                    "error": outcome.error,
                    "date": datetime.now(UTC).isoformat(),
                },
            )
            raise RuntimeError(outcome.error)

        raise RuntimeError(f"max_steps_reached={self.settings.max_steps}")

    def _load_skills_prompt(self, prompt: str, workspace: Path, allowed_skills: set[str] | None = None) -> str:
        skill_index = {
            skill.name.casefold(): skill
            for skill in discover_skills(workspace)
            if allowed_skills is None or skill.name.casefold() in allowed_skills
        }
        expanded_skills = set(HINT_RE.findall(prompt)) & set(skill_index.keys())
        return render_skills_prompt(list(skill_index.values()), expanded_skills=expanded_skills)

    async def _run_tools_once(
        self,
        *,
        tape: Tape,
        prompt: str | list[dict],
        model: str | None = None,
        allowed_tools: Collection[str] | None = None,
        allowed_skills: Collection[str] | None = None,
    ) -> ToolAutoResult:
        extra_options = {"extra_headers": DEFAULT_BUB_HEADERS} if self.settings.model.startswith("openrouter:") else {}
        prompt_text = prompt if isinstance(prompt, str) else _extract_text_from_parts(prompt)
        if allowed_tools is not None:
            allowed_tools = {name.casefold() for name in allowed_tools}
        if allowed_skills is not None:
            allowed_skills = {name.casefold() for name in allowed_skills}
            tape.context.state["allowed_skills"] = list(allowed_skills)
        if allowed_tools is not None:
            tools = [tool for tool in REGISTRY.values() if tool.name.casefold() in allowed_tools]
        else:
            tools = list(REGISTRY.values())
        system_prompt = self._system_prompt(prompt_text, state=tape.context.state, allowed_skills=allowed_skills)
        normalized_prompt = _normalize_prompt_for_api_format(prompt, self.settings.api_format)
        async with asyncio.timeout(self.settings.model_timeout_seconds):
            if _is_internal_continue_prompt(prompt):
                return await _run_tools_without_persisting_prompt(
                    tape=tape,
                    prompt=normalized_prompt,
                    system_prompt=system_prompt,
                    tools=model_tools(tools),
                    max_tokens=self.settings.max_tokens,
                    model=model,
                    state=tape.context.state,
                    extra_options=extra_options,
                )
            if _prompt_contains_image_parts(normalized_prompt):
                return await _run_tools_with_transient_prompt(
                    tape=tape,
                    prompt=normalized_prompt,
                    persisted_prompt=_prompt_for_tape(prompt),
                    system_prompt=system_prompt,
                    tools=model_tools(tools),
                    max_tokens=self.settings.max_tokens,
                    model=model,
                    state=tape.context.state,
                    extra_options=extra_options,
                )
            return await tape.run_tools_async(
                prompt=normalized_prompt,
                system_prompt=system_prompt,
                max_tokens=self.settings.max_tokens,
                tools=model_tools(tools),
                model=model,
                **extra_options,
            )

    def _system_prompt(self, prompt: str, state: State, allowed_skills: set[str] | None = None) -> str:
        blocks: list[str] = []
        if result := self.framework.get_system_prompt(prompt=prompt, state=state):
            blocks.append(result)
        tools_prompt = render_tools_prompt(REGISTRY.values())
        if tools_prompt:
            blocks.append(tools_prompt)
        workspace = workspace_from_state(state)
        if skills_prompt := self._load_skills_prompt(prompt, workspace, allowed_skills):
            blocks.append(skills_prompt)
        return "\n\n".join(blocks)


@dataclass(frozen=True)
class _ToolAutoOutcome:
    kind: str
    text: str = ""
    error: str = ""


def _resolve_tool_auto_result(output: ToolAutoResult) -> _ToolAutoOutcome:
    if output.kind == "text":
        return _ToolAutoOutcome(kind="text", text=output.text or "")
    if output.kind == "tools" or output.tool_calls or output.tool_results:
        return _ToolAutoOutcome(kind="continue")
    if output.error is None:
        return _ToolAutoOutcome(kind="error", error="tool_auto_error: unknown")
    error_kind = getattr(output.error.kind, "value", str(output.error.kind))
    return _ToolAutoOutcome(kind="error", error=f"{error_kind}: {output.error.message}")


def _build_llm(settings: AgentSettings, tape_store: AsyncTapeStore) -> LLM:
    from republic.auth.openai_codex import openai_codex_oauth_resolver

    return LLM(
        settings.model,
        api_key=settings.api_key,
        api_base=settings.api_base,
        fallback_models=[settings.fallback_model] if settings.fallback_model else None,
        api_key_resolver=openai_codex_oauth_resolver(),
        tape_store=tape_store,
        api_format=settings.api_format,
        context=default_tape_context(),
    )


def _load_runtime_settings() -> AgentSettings:
    return AgentSettings.from_env()


@dataclass(frozen=True)
class Args:
    positional: list[str]
    kwargs: dict[str, Any]


def _parse_internal_command(line: str) -> tuple[str, list[str]]:
    body = line.strip()
    words = shlex.split(body)
    if not words:
        return "", []
    return words[0], words[1:]


def _parse_args(args_tokens: list[str]) -> Args:
    positional: list[str] = []
    kwargs: dict[str, str] = {}
    first_kwarg = False
    for token in args_tokens:
        if "=" in token:
            key, value = token.split("=", 1)
            kwargs[key] = value
            first_kwarg = True
        elif first_kwarg:
            raise ValueError(f"positional argument '{token}' cannot appear after keyword arguments")
        else:
            positional.append(token)
    return Args(positional=positional, kwargs=kwargs)


def _extract_text_from_parts(parts: list[dict]) -> str:
    """Extract text content from multimodal content parts."""
    return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _continue_prompt(state: State) -> str | list[dict[str, object]]:
    prompt_text = f"{CONTINUE_PROMPT} [context: {state['context']}]" if "context" in state else CONTINUE_PROMPT
    media_parts = _persistent_inbound_media_parts(state) + _pop_pending_tool_media_parts(state)
    if not media_parts:
        return prompt_text
    return [{"type": "text", "text": prompt_text}, *media_parts]


def _persistent_inbound_media_parts(state: State) -> list[dict[str, object]]:
    return _coerce_image_parts(state.get("_inbound_media_parts"))


def _pop_pending_tool_media_parts(state: State) -> list[dict[str, object]]:
    return _coerce_image_parts(state.pop("_tool_media_parts", None))


def _coerce_image_parts(raw: object) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return []
    parts: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        part_type = item.get("type")
        image_url = item.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if not isinstance(url, str) or not url:
            continue
        if part_type not in {"image_url", "input_image"}:
            continue
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _normalize_prompt_for_api_format(
    prompt: str | list[dict[str, object]],
    api_format: str,
) -> str | list[dict[str, object]]:
    if api_format != "responses" or isinstance(prompt, str):
        return prompt
    normalized_parts: list[dict[str, object]] = []
    for part in prompt:
        if not isinstance(part, dict):
            continue
        normalized = _normalize_prompt_part_for_responses(part)
        if normalized is not None:
            normalized_parts.append(normalized)
    return normalized_parts


def _normalize_prompt_part_for_responses(part: dict[str, object]) -> dict[str, object] | None:
    part_type = part.get("type")
    if part_type == "input_text" or part_type == "input_image":
        return dict(part)
    if part_type == "text":
        text = part.get("text")
        if isinstance(text, str):
            return {"type": "input_text", "text": text}
        return None
    if part_type == "image_url":
        image_url = part.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if isinstance(url, str) and url:
            return {"type": "input_image", "image_url": url, "detail": "auto"}
        return None
    return dict(part)


def _prompt_contains_image_parts(prompt: str | list[dict[str, object]]) -> bool:
    if isinstance(prompt, str):
        return False
    return any(isinstance(part, dict) and part.get("type") in {"image_url", "input_image"} for part in prompt)


def _prompt_for_tape(prompt: str | list[dict[str, object]]) -> str:
    if isinstance(prompt, str):
        return prompt
    text = _extract_text_from_parts(prompt).strip()
    image_count = sum(
        1 for part in prompt if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}
    )
    if image_count == 0:
        return text
    omission = f"[{image_count} image{'s' if image_count != 1 else ''} omitted from tape history]"
    if text:
        return f"{text}\n\n{omission}"
    return omission


def _is_internal_continue_prompt(prompt: str | list[dict[str, object]]) -> bool:
    if isinstance(prompt, str):
        return prompt.startswith(CONTINUE_PROMPT)
    if not prompt:
        return False
    first = prompt[0]
    if not isinstance(first, dict):
        return False
    text = first.get("text")
    return isinstance(text, str) and text.startswith(CONTINUE_PROMPT)


def _event_prompt(prompt: str | list[dict[str, object]]) -> str | list[dict[str, object]]:
    if isinstance(prompt, str):
        return prompt
    summary: list[dict[str, object]] = []
    for part in prompt:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"image_url", "input_image"}:
            summary.append({"type": str(part_type), "redacted": True})
            continue
        summary.append(dict(part))
    return summary


async def _run_tools_without_persisting_prompt(
    *,
    tape: Tape,
    prompt: str | list[dict[str, object]],
    system_prompt: str,
    tools: list[Any],
    max_tokens: int,
    model: str | None,
    state: State,
    extra_options: dict[str, object],
) -> ToolAutoResult:
    return await _run_tools_with_transient_prompt(
        tape=tape,
        prompt=prompt,
        persisted_prompt=None,
        system_prompt=system_prompt,
        tools=tools,
        max_tokens=max_tokens,
        model=model,
        state=state,
        extra_options=extra_options,
    )


async def _run_tools_with_transient_prompt(
    *,
    tape: Tape,
    prompt: str | list[dict[str, object]],
    persisted_prompt: str | None,
    system_prompt: str,
    tools: list[Any],
    max_tokens: int,
    model: str | None,
    state: State,
    extra_options: dict[str, object],
) -> ToolAutoResult:
    history = await tape.read_messages_async()
    transient_user_message = {"role": "user", "content": prompt}
    messages = _messages_with_system_prompt(history, system_prompt, transient_user_message)
    client = getattr(tape, "_client", None)
    prepare_request = getattr(client, "_prepare_request_async", None)
    execute = getattr(client, "_execute_async", None)
    if (
        client is None
        or not callable(prepare_request)
        or not callable(execute)
        or not inspect.iscoroutinefunction(prepare_request)
        or not inspect.iscoroutinefunction(execute)
    ):
        return await tape.run_tools_async(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            tools=tools,
            model=model,
            **extra_options,
        )
    prepared = await prepare_request(
        prompt=None,
        system_prompt=None,
        messages=messages,
        tape=None,
        context=tape.context,
        tools=tools,
        require_tools=True,
        require_runnable=True,
    )
    new_messages = [_persisted_user_message(persisted_prompt, state)] if persisted_prompt is not None else []
    prepared = replace(
        prepared,
        tape=tape.name,
        should_update=True,
        new_messages=new_messages,
        system_prompt=system_prompt if new_messages else None,
    )
    return await execute(
        prepared,
        tools_payload=prepared.toolset.payload,
        model=model,
        provider=None,
        max_tokens=max_tokens,
        stream=False,
        kwargs=dict(extra_options),
        on_response=partial(client._handle_tools_auto_response_async, prepared),
    )


def _messages_with_system_prompt(
    history: list[dict[str, Any]],
    system_prompt: str,
    transient_user_message: dict[str, object],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(history)
    messages.append(transient_user_message)
    return messages


def _persisted_user_message(persisted_prompt: str, state: State) -> dict[str, object]:
    message: dict[str, object] = {"role": "user", "content": persisted_prompt}
    resource_refs = coerce_resource_refs(state.get("_inbound_resource_refs", state.get("_inbound_media_refs")))
    if resource_refs:
        message[RESOURCE_REFS_KEY] = resource_refs
    inbound_message_id = state.get("_inbound_message_id")
    if isinstance(inbound_message_id, str) and inbound_message_id:
        message[INBOUND_MESSAGE_ID_KEY] = inbound_message_id
    return message


def _coerce_media_refs(raw: object) -> list[dict[str, str]]:
    refs = coerce_resource_refs(raw)
    normalized: list[dict[str, str]] = []
    for ref in refs:
        locator = ref.get("locator")
        if not isinstance(locator, dict):
            continue
        current: dict[str, str] = {}
        channel = locator.get("channel")
        if isinstance(channel, str) and channel:
            current["channel"] = channel
        elif locator.get("kind") == "url":
            current["channel"] = "unknown"
        else:
            continue
        for key in ("message_id", "file_key", "resource_type", "url"):
            value = locator.get(key)
            if isinstance(value, str) and value:
                current[key] = value
        for key in ("content_type", "name"):
            value = ref.get(key)
            if isinstance(value, str) and value:
                current[key] = value
        if "url" in current or {"message_id", "file_key", "resource_type"} <= set(current):
            normalized.append(current)
    return normalized
