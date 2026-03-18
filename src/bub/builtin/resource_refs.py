from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal, TypedDict, cast
from urllib.parse import urlsplit, urlunsplit

LEGACY_MEDIA_REFS_KEY = "_bub_media_refs"
RESOURCE_REFS_KEY = "_bub_resource_refs"
MAX_RESOURCE_REFS = 8

type ResourceKind = Literal["image", "audio", "video", "file", "pdf", "trace", "profile", "har", "state", "unknown"]
type ResourceScope = Literal["message", "quote", "tool", "unknown"]


class ResourceLocator(TypedDict, total=False):
    kind: str
    path: str
    url: str
    channel: str
    message_id: str
    file_key: str
    resource_type: str


class ResourceRef(TypedDict, total=False):
    kind: str
    scope: str
    origin_role: str
    origin_name: str
    content_type: str
    name: str
    locator: ResourceLocator
    meta: dict[str, object]


def coerce_resource_refs(raw: object) -> list[ResourceRef]:
    if not isinstance(raw, list):
        return []
    refs: list[ResourceRef] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        ref = _normalize_resource_ref(item)
        if ref is not None:
            refs.append(ref)
        if len(refs) >= MAX_RESOURCE_REFS:
            break
    return refs


def clone_resource_refs(refs: list[ResourceRef]) -> list[ResourceRef]:
    return [cast(ResourceRef, dict(ref)) for ref in refs]


def summarize_resource_ref(ref: Mapping[str, object]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for key in ("kind", "scope", "content_type", "name"):
        value = ref.get(key)
        if isinstance(value, str) and value:
            summary[key] = value
    locator = ref.get("locator")
    if isinstance(locator, Mapping):
        locator_kind = locator.get("kind")
        if isinstance(locator_kind, str) and locator_kind:
            summary["locator_kind"] = locator_kind
        channel = locator.get("channel")
        if isinstance(channel, str) and channel:
            summary["channel"] = channel
    origin_name = ref.get("origin_name")
    if isinstance(origin_name, str) and origin_name:
        summary["origin"] = origin_name
    return summary


def resource_ref_signature(ref: Mapping[str, object]) -> str:
    return json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)


def resource_refs_from_artifacts(raw: object, *, origin_name: str | None = None) -> list[ResourceRef]:
    if not isinstance(raw, list):
        return []
    refs: list[ResourceRef] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        ref = _resource_ref_from_artifact(item, origin_name=origin_name)
        if ref is not None:
            refs.append(ref)
        if len(refs) >= MAX_RESOURCE_REFS:
            break
    return refs


def _normalize_resource_ref(raw: Mapping[str, object]) -> ResourceRef | None:
    locator_value = raw.get("locator")
    if isinstance(locator_value, Mapping):
        locator = _normalize_locator(locator_value)
        if locator is None:
            return None
        ref: ResourceRef = {
            "kind": _normalize_kind(raw.get("kind"), content_type=raw.get("content_type")),
            "scope": _normalize_scope(raw.get("scope")),
            "locator": locator,
        }
        _set_string(ref, "origin_role", raw.get("origin_role"))
        _set_string(ref, "origin_name", raw.get("origin_name"))
        _set_string(ref, "content_type", raw.get("content_type"))
        _set_string(ref, "name", raw.get("name"))
        meta = raw.get("meta")
        if isinstance(meta, Mapping):
            ref["meta"] = {str(key): value for key, value in meta.items()}
        return ref
    return _resource_ref_from_legacy_media_ref(raw)


def _resource_ref_from_legacy_media_ref(raw: Mapping[str, object]) -> ResourceRef | None:
    content_type = _string_or_none(raw.get("content_type"))
    kind = _normalize_kind(raw.get("kind"), content_type=content_type)
    locator: ResourceLocator
    url = _string_or_none(raw.get("url"))
    if url is not None:
        locator = {"kind": "url", "url": _sanitize_url(url)}
    else:
        channel = _string_or_none(raw.get("channel"))
        message_id = _string_or_none(raw.get("message_id"))
        file_key = _string_or_none(raw.get("file_key"))
        resource_type = _string_or_none(raw.get("resource_type"))
        if channel is None or message_id is None or file_key is None or resource_type is None:
            return None
        locator = {
            "kind": "channel_file",
            "channel": channel,
            "message_id": message_id,
            "file_key": file_key,
            "resource_type": resource_type,
        }
    ref: ResourceRef = {"kind": kind, "scope": "message", "locator": locator}
    if content_type is not None:
        ref["content_type"] = content_type
    name = _string_or_none(raw.get("name"))
    if name is not None:
        ref["name"] = name
    return ref


def _resource_ref_from_artifact(raw: Mapping[str, object], *, origin_name: str | None) -> ResourceRef | None:
    content_type = _string_or_none(raw.get("content_type"))
    path = _string_or_none(raw.get("path"))
    url = _string_or_none(raw.get("url"))
    locator: ResourceLocator | None = None
    if path is not None:
        locator = {"kind": "path", "path": path}
    elif url is not None:
        locator = {"kind": "url", "url": _sanitize_url(url)}
    if locator is None:
        return None
    ref: ResourceRef = {
        "kind": _normalize_kind(raw.get("kind"), content_type=content_type),
        "scope": "tool",
        "origin_role": "tool",
        "locator": locator,
    }
    if origin_name:
        ref["origin_name"] = origin_name
    if content_type is not None:
        ref["content_type"] = content_type
    name = _string_or_none(raw.get("name"))
    if name is not None:
        ref["name"] = name
    meta: dict[str, object] = {}
    for key in ("session", "transport", "inline_transport", "base64_omitted"):
        if key in raw:
            meta[key] = raw[key]
    if meta:
        ref["meta"] = meta
    return ref


def _normalize_locator(raw: Mapping[str, object]) -> ResourceLocator | None:
    kind = _string_or_none(raw.get("kind"))
    if kind is None:
        return None
    locator: ResourceLocator = {"kind": kind}
    for key in ("path", "channel", "message_id", "file_key", "resource_type"):
        value = _string_or_none(raw.get(key))
        if value is not None:
            locator[key] = value
    url = _string_or_none(raw.get("url"))
    if url is not None:
        locator["url"] = _sanitize_url(url)
    if kind == "url" and "url" not in locator:
        return None
    if kind == "path" and "path" not in locator:
        return None
    if kind == "channel_file" and not {"channel", "message_id", "file_key", "resource_type"} <= set(locator):
        return None
    return locator


def _normalize_kind(value: object, *, content_type: object) -> str:
    text = _string_or_none(value)
    if text is not None:
        return text
    content_type_text = _string_or_none(content_type) or ""
    lowered = content_type_text.lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("audio/"):
        return "audio"
    if lowered.startswith("video/"):
        return "video"
    if lowered == "application/pdf":
        return "pdf"
    if lowered:
        return "file"
    return "unknown"


def _normalize_scope(value: object) -> str:
    text = _string_or_none(value)
    if text is None:
        return "unknown"
    return text


def _sanitize_url(value: str) -> str:
    if value.startswith("data:") or value.startswith("file://"):
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return value
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _set_string(target: dict[str, object], key: str, value: object) -> None:
    text = _string_or_none(value)
    if text is not None:
        target[key] = text


def _string_or_none(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped if stripped else None
