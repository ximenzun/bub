"""Tape context helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Literal, cast

from republic import TapeContext, TapeEntry
from republic.tape.context import LAST_ANCHOR, AnchorSelector

from bub.builtin.resource_refs import (
    LEGACY_MEDIA_REFS_KEY,
    RESOURCE_REFS_KEY,
    ResourceRef,
    coerce_resource_refs,
    resource_ref_signature,
    resource_refs_from_artifacts,
    summarize_resource_ref,
)

type TapeView = Literal["active", "messages", "timeline"]

DEFAULT_TAPE_VIEW: TapeView = "active"
TAPE_VIEW_KEY = "_tape_view"
TAPE_ANCHOR_NAME_KEY = "_tape_anchor_name"
TAPE_ANCHOR_STATE_KEY = "_tape_anchor_state"
AVAILABLE_TAPE_VIEWS: tuple[TapeView, ...] = ("active", "messages", "timeline")
DATA_URL_RE = re.compile(r"data:(?P<mime>[^;,]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)", re.IGNORECASE)


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
    messages, _resources = select_messages_and_resources(entries, context)
    return messages


def select_messages_and_resources(
    entries: Iterable[TapeEntry],
    context: TapeContext,
) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    entry_list = list(entries)
    view = normalize_tape_view(context.state.get(TAPE_VIEW_KEY))
    if view == "timeline":
        return _select_timeline_entries_and_resources(entry_list)

    messages: list[dict[str, Any]] = []
    resources: list[ResourceRef] = []
    pending_calls: list[dict[str, Any]] = []
    if view == "active":
        _prepend_anchor_state(messages, context)

    for entry in entry_list:
        if entry.kind == "message":
            _append_message_entry(messages, entry)
            resources.extend(_resource_refs_from_message_entry(entry))
            continue

        if entry.kind == "tool_call":
            pending_calls = _append_tool_call_entry(messages, entry)
            continue

        if entry.kind == "tool_result":
            _append_tool_result_entry(messages, pending_calls, entry)
            resources.extend(_resource_refs_from_tool_result_entry(entry, pending_calls))
            pending_calls = []

    return messages, _dedupe_resource_refs(resources)


def _append_message_entry(messages: list[dict[str, Any]], entry: TapeEntry) -> None:
    payload = entry.payload
    if isinstance(payload, dict):
        messages.append(_sanitize_message_payload(payload))


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
        return _sanitize_tool_result_string(result)
    try:
        return json.dumps(_sanitize_for_context(result), ensure_ascii=False)
    except TypeError:
        return _sanitize_tool_result_string(str(result))


def _sanitize_message_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    message = dict(payload)
    message.pop(RESOURCE_REFS_KEY, None)
    message.pop(LEGACY_MEDIA_REFS_KEY, None)
    message.pop("_bub_media_refs", None)
    message.pop("_bub_inbound_message_id", None)
    message["content"] = _sanitize_message_content(message.get("content"))
    return message


def _sanitize_message_content(content: object) -> object:
    if not isinstance(content, list):
        return _sanitize_tool_result_string(content) if isinstance(content, str) else content

    text_parts: list[str] = []
    image_count = 0
    sanitized_parts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        part_type = item.get("type")
        if part_type in {"text", "input_text"}:
            text = item.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
            sanitized_parts.append(dict(item))
            continue
        if part_type in {"image_url", "input_image"}:
            image_count += 1
            continue
        sanitized_parts.append(cast(dict[str, Any], _sanitize_for_context(item)))

    if image_count == 0:
        return sanitized_parts

    omission = f"[{image_count} image{'s' if image_count != 1 else ''} omitted from tape history]"
    text = "\n".join(part for part in text_parts if part).strip()
    if text:
        return f"{text}\n\n{omission}"
    return omission


def _sanitize_tool_result_string(result: str) -> str:
    if result[:1] in "{[":
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return _replace_data_urls(result)
        return json.dumps(_sanitize_for_context(parsed), ensure_ascii=False)
    return _replace_data_urls(result)


def _sanitize_for_context(value: object) -> object:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in {RESOURCE_REFS_KEY, LEGACY_MEDIA_REFS_KEY}:
                sanitized[str(key)] = _resource_ref_summaries(coerce_resource_refs(item))
                continue
            if str(key) == "artifacts":
                sanitized[str(key)] = _resource_ref_summaries(resource_refs_from_artifacts(item))
                continue
            if str(key).casefold() == "base64" and isinstance(item, str):
                sanitized[str(key)] = f"[base64 omitted: {len(item)} chars]"
                continue
            sanitized[str(key)] = _sanitize_for_context(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_for_context(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_for_context(item) for item in value]
    if isinstance(value, str):
        return _replace_data_urls(value)
    return value


def _replace_data_urls(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        mime_type = match.group("mime")
        encoded = match.group("data")
        return f"[data URL omitted: {mime_type}; {len(encoded)} chars]"

    return DATA_URL_RE.sub(replace, value)


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


def _select_timeline_entries_and_resources(
    entries: list[TapeEntry],
) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    messages: list[dict[str, Any]] = []
    resources: list[ResourceRef] = []
    pending_calls: list[dict[str, Any]] = []

    for entry in entries:
        if entry.kind == "message":
            _append_message_entry(messages, entry)
            resources.extend(_resource_refs_from_message_entry(entry))
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
            resources.extend(_resource_refs_from_tool_result_entry(entry, pending_calls))
            pending_calls = []

    return messages, _dedupe_resource_refs(resources)


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


def _resource_refs_from_message_entry(entry: TapeEntry) -> list[ResourceRef]:
    payload = entry.payload
    if not isinstance(payload, Mapping):
        return []
    return coerce_resource_refs(payload.get(RESOURCE_REFS_KEY, payload.get(LEGACY_MEDIA_REFS_KEY)))


def _resource_refs_from_tool_result_entry(
    entry: TapeEntry,
    pending_calls: list[dict[str, Any]],
) -> list[ResourceRef]:
    results = entry.payload.get("results")
    if not isinstance(results, list):
        return []
    refs: list[ResourceRef] = []
    for index, result in enumerate(results):
        origin_name = None
        if index < len(pending_calls):
            function = pending_calls[index].get("function")
            if isinstance(function, Mapping):
                name = function.get("name")
                if isinstance(name, str) and name:
                    origin_name = name
        refs.extend(_resource_refs_from_tool_result(result, origin_name=origin_name))
    return refs


def _resource_refs_from_tool_result(result: object, *, origin_name: str | None) -> list[ResourceRef]:
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return []
        return _resource_refs_from_tool_result(parsed, origin_name=origin_name)
    if isinstance(result, Mapping):
        refs = coerce_resource_refs(result.get(RESOURCE_REFS_KEY, result.get(LEGACY_MEDIA_REFS_KEY)))
        if refs:
            return _with_tool_origin(refs, origin_name=origin_name)
        artifacts = resource_refs_from_artifacts(result.get("artifacts"), origin_name=origin_name)
        if artifacts:
            return artifacts
        return []
    if isinstance(result, list):
        refs: list[ResourceRef] = []
        for item in result:
            refs.extend(_resource_refs_from_tool_result(item, origin_name=origin_name))
        return refs
    return []


def _with_tool_origin(refs: list[ResourceRef], *, origin_name: str | None) -> list[ResourceRef]:
    normalized: list[ResourceRef] = []
    for ref in refs:
        current = dict(ref)
        current.setdefault("scope", "tool")
        current.setdefault("origin_role", "tool")
        if origin_name is not None:
            current.setdefault("origin_name", origin_name)
        normalized.append(cast(ResourceRef, current))
    return normalized


def _dedupe_resource_refs(refs: list[ResourceRef]) -> list[dict[str, object]]:
    seen: set[str] = set()
    deduped: list[dict[str, object]] = []
    for ref in refs:
        signature = resource_ref_signature(ref)
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(dict(ref))
    return deduped


def _resource_ref_summaries(refs: list[ResourceRef]) -> list[dict[str, object]]:
    return [summarize_resource_ref(ref) for ref in cast(list[ResourceRef], _dedupe_resource_refs(refs))]
