"""Tape context helpers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Literal, cast

from republic import TapeContext, TapeEntry
from republic.tape.context import LAST_ANCHOR, AnchorSelector

type TapeView = Literal["active", "messages", "timeline"]

DEFAULT_TAPE_VIEW: TapeView = "active"
TAPE_VIEW_KEY = "_tape_view"
TAPE_ANCHOR_NAME_KEY = "_tape_anchor_name"
TAPE_ANCHOR_STATE_KEY = "_tape_anchor_state"
AVAILABLE_TAPE_VIEWS: tuple[TapeView, ...] = ("active", "messages", "timeline")


def available_tape_views() -> tuple[TapeView, ...]:
    return AVAILABLE_TAPE_VIEWS


def normalize_tape_view(value: object) -> TapeView:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in AVAILABLE_TAPE_VIEWS:
            return cast(TapeView, normalized)
    return DEFAULT_TAPE_VIEW


def build_tape_context(
    *,
    state: dict[str, Any] | None = None,
    view: TapeView = DEFAULT_TAPE_VIEW,
    anchor: AnchorSelector = LAST_ANCHOR,
) -> TapeContext:
    next_state = dict(state or {})
    next_state[TAPE_VIEW_KEY] = normalize_tape_view(view)
    return TapeContext(anchor=anchor, select=_select_messages, state=next_state)


def default_tape_context(state: dict[str, Any] | None = None) -> TapeContext:
    """Return the default context selection for Bub."""

    return build_tape_context(state=state)


def restore_tape_state(
    runtime_state: dict[str, Any] | None,
    *,
    anchor_name: str | None,
    anchor_state: dict[str, Any] | None,
    view: TapeView = DEFAULT_TAPE_VIEW,
) -> dict[str, Any]:
    restored = dict(anchor_state or {})
    if runtime_state:
        restored.update(runtime_state)
    restored[TAPE_VIEW_KEY] = normalize_tape_view(restored.get(TAPE_VIEW_KEY, view))
    restored[TAPE_ANCHOR_NAME_KEY] = anchor_name
    restored[TAPE_ANCHOR_STATE_KEY] = dict(anchor_state or {})
    return restored


def _select_messages(entries: Iterable[TapeEntry], context: TapeContext) -> list[dict[str, Any]]:
    entry_list = list(entries)
    view = normalize_tape_view(context.state.get(TAPE_VIEW_KEY))
    if view == "timeline":
        return _select_timeline_entries(entry_list)

    messages: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []
    if view == "active":
        _prepend_anchor_state(messages, context)

    for entry in entry_list:
        if entry.kind == "message":
            _append_message_entry(messages, entry)
            continue

        if entry.kind == "tool_call":
            pending_calls = _append_tool_call_entry(messages, entry)
            continue

        if entry.kind == "tool_result":
            _append_tool_result_entry(messages, pending_calls, entry)
            pending_calls = []

    return messages


def _append_message_entry(messages: list[dict[str, Any]], entry: TapeEntry) -> None:
    payload = entry.payload
    if isinstance(payload, dict):
        messages.append(dict(payload))


def _append_tool_call_entry(messages: list[dict[str, Any]], entry: TapeEntry) -> list[dict[str, Any]]:
    calls = _normalize_tool_calls(entry.payload.get("calls"))
    if calls:
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
    return calls


def _append_tool_result_entry(
    messages: list[dict[str, Any]],
    pending_calls: list[dict[str, Any]],
    entry: TapeEntry,
) -> None:
    results = entry.payload.get("results")
    if not isinstance(results, list):
        return
    for index, result in enumerate(results):
        messages.append(_build_tool_result_message(result, pending_calls, index))


def _build_tool_result_message(
    result: object,
    pending_calls: list[dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "tool", "content": _render_tool_result(result)}
    if index >= len(pending_calls):
        return message

    call = pending_calls[index]
    call_id = call.get("id")
    if isinstance(call_id, str) and call_id:
        message["tool_call_id"] = call_id

    function = call.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name:
            message["name"] = name
    return message


def _normalize_tool_calls(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            calls.append(dict(item))
    return calls


def _render_tool_result(result: object) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except TypeError:
        return str(result)


def _prepend_anchor_state(messages: list[dict[str, Any]], context: TapeContext) -> None:
    anchor_name = context.state.get(TAPE_ANCHOR_NAME_KEY)
    anchor_state = context.state.get(TAPE_ANCHOR_STATE_KEY)
    if not isinstance(anchor_state, dict) or not anchor_state:
        return
    if anchor_name == "session/start" and "summary" not in anchor_state:
        return

    summary = anchor_state.get("summary")
    lines: list[str] = []
    if isinstance(anchor_name, str) and anchor_name:
        lines.append(f"Tape anchor: {anchor_name}")
    if isinstance(summary, str) and summary.strip():
        lines.append(f"Summary: {summary.strip()}")
    extra_state = {key: value for key, value in anchor_state.items() if key != "summary"}
    if extra_state:
        lines.append(f"State: {json.dumps(extra_state, ensure_ascii=False, sort_keys=True, default=str)}")
    if lines:
        messages.append({"role": "system", "content": "\n".join(lines)})


def _select_timeline_entries(entries: list[TapeEntry]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    pending_calls: list[dict[str, Any]] = []

    for entry in entries:
        if entry.kind == "message":
            _append_message_entry(messages, entry)
            continue
        if entry.kind == "system":
            messages.append({"role": "system", "content": _entry_text(entry.payload.get("content"))})
            continue
        if entry.kind == "anchor":
            messages.append({"role": "system", "content": _render_anchor_entry(entry)})
            continue
        if entry.kind == "event":
            messages.append({"role": "system", "content": _render_named_entry("event", entry)})
            continue
        if entry.kind == "error":
            messages.append({"role": "system", "content": _render_named_entry("error", entry)})
            continue
        if entry.kind == "tool_call":
            pending_calls = _append_tool_call_entry(messages, entry)
            continue
        if entry.kind == "tool_result":
            _append_tool_result_entry(messages, pending_calls, entry)
            pending_calls = []

    return messages


def _render_anchor_entry(entry: TapeEntry) -> str:
    anchor_name = _entry_text(entry.payload.get("name")) or "-"
    state = entry.payload.get("state")
    lines = [f"[anchor] {anchor_name}"]
    if isinstance(state, dict) and state:
        lines.append(json.dumps(state, ensure_ascii=False, sort_keys=True, default=str))
    return "\n".join(lines)


def _render_named_entry(kind: str, entry: TapeEntry) -> str:
    payload = json.dumps(entry.payload, ensure_ascii=False, sort_keys=True, default=str)
    name = _entry_text(entry.payload.get("name"))
    if name:
        return f"[{kind}:{name}] {payload}"
    return f"[{kind}] {payload}"


def _entry_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(value)
