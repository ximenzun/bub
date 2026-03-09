"""Structured social-channel abstractions shared by platform adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from typing import Any, Literal

type ConversationSurface = Literal["direct", "group", "channel", "thread", "business", "unknown"]
type EventKind = Literal["message", "interaction", "reaction", "read_receipt", "lifecycle"]
type ContentKind = Literal["text", "rich_text", "card", "image", "audio", "video", "file", "json", "unknown"]
type ReplyMode = Literal["none", "same_conversation", "message_id", "token", "windowed"]
type ProgressSurface = Literal["presence", "text_draft", "card_stream", "follow_up"]
type ActionKind = Literal[
    "send_message",
    "reply_message",
    "edit_message",
    "patch_message",
    "update_card",
    "stream_card",
    "append_follow_up",
    "delete_message",
    "set_reaction",
    "pin_message",
    "mark_read",
    "presence",
    "escalate_message",
    "custom",
]


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _mapping_of(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def normalize_surface(value: Any) -> ConversationSurface:
    normalized = str(value or "unknown").strip().lower()
    alias_map: dict[str, ConversationSurface] = {
        "direct": "direct",
        "dm": "direct",
        "private": "direct",
        "p2p": "direct",
        "group": "group",
        "supergroup": "group",
        "channel": "channel",
        "guild": "channel",
        "thread": "thread",
        "topic": "thread",
        "business": "business",
        "business_dm": "business",
        "unknown": "unknown",
    }
    return alias_map.get(normalized, "unknown")


def to_primitive(value: Any) -> Any:
    """Convert nested dataclasses into JSON-serializable primitives."""

    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value):
        return {key: to_primitive(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_primitive(item) for item in value]
    return value


@dataclass(slots=True)
class ConversationRef:
    platform: str
    chat_id: str
    account_id: str = "default"
    surface: ConversationSurface = "unknown"
    thread_id: str | None = None
    lane_id: str | None = None
    actor_id: str | None = None
    tenant_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        default_platform: str | None = None,
        default_chat_id: str = "default",
    ) -> ConversationRef:
        return cls(
            platform=str(data.get("platform") or default_platform or "unknown"),
            chat_id=str(data.get("chat_id") or default_chat_id),
            account_id=str(data.get("account_id") or "default"),
            surface=normalize_surface(data.get("surface")),
            thread_id=_as_str(data.get("thread_id")),
            lane_id=_as_str(data.get("lane_id")),
            actor_id=_as_str(data.get("actor_id")),
            tenant_id=_as_str(data.get("tenant_id")),
            metadata=dict(_mapping_of(data.get("metadata"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(slots=True)
class ParticipantRef:
    id: str
    id_kind: str = "opaque"
    display_name: str | None = None
    username: str | None = None
    is_bot: bool | None = None
    tenant_id: str | None = None
    open_id: str | None = None
    union_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ParticipantRef:
        return cls(
            id=str(data.get("id") or data.get("sender_id") or data.get("user_id") or "unknown"),
            id_kind=str(data.get("id_kind") or "opaque"),
            display_name=_as_str(data.get("display_name") or data.get("full_name") or data.get("name")),
            username=_as_str(data.get("username")),
            is_bot=data.get("is_bot"),
            tenant_id=_as_str(data.get("tenant_id")),
            open_id=_as_str(data.get("open_id")),
            union_id=_as_str(data.get("union_id")),
            metadata=dict(_mapping_of(data.get("metadata"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(slots=True)
class ReplyGrant:
    mode: ReplyMode = "none"
    reply_to_message_id: str | None = None
    token: str | None = None
    expires_at: datetime | None = None
    max_uses: int | None = None
    edit_until: datetime | None = None
    delete_until: datetime | None = None
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ReplyGrant:
        mode = str(data.get("mode") or "none")
        if mode not in {"none", "same_conversation", "message_id", "token", "windowed"}:
            mode = "none"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            reply_to_message_id=_as_str(data.get("reply_to_message_id")),
            token=_as_str(data.get("token")),
            expires_at=_coerce_datetime(data.get("expires_at")),
            max_uses=int(data["max_uses"]) if data.get("max_uses") is not None else None,
            edit_until=_coerce_datetime(data.get("edit_until")),
            delete_until=_coerce_datetime(data.get("delete_until")),
            thread_id=_as_str(data.get("thread_id")),
            metadata=dict(_mapping_of(data.get("metadata"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(slots=True)
class Attachment:
    content_type: str
    url: str | None = None
    name: str | None = None
    file_key: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Attachment:
        return cls(
            content_type=str(data.get("content_type") or data.get("mime_type") or "application/octet-stream"),
            url=_as_str(data.get("url") or data.get("data_url")),
            name=_as_str(data.get("name") or data.get("file_name")),
            file_key=_as_str(data.get("file_key") or data.get("file_id")),
            size=int(data["size"]) if data.get("size") is not None else data.get("file_size"),
            width=int(data["width"]) if data.get("width") is not None else None,
            height=int(data["height"]) if data.get("height") is not None else None,
            duration_ms=int(data["duration_ms"]) if data.get("duration_ms") is not None else None,
            metadata=dict(_mapping_of(data.get("metadata"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(slots=True)
class LiveSurfaceRef:
    mode: ProgressSurface
    surface_id: str | None = None
    parent_message_id: str | None = None
    sequence: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> LiveSurfaceRef:
        mode = str(data.get("mode") or "presence")
        if mode not in {"presence", "text_draft", "card_stream", "follow_up"}:
            mode = "presence"
        return cls(
            mode=mode,  # type: ignore[arg-type]
            surface_id=_as_str(data.get("surface_id")),
            parent_message_id=_as_str(data.get("parent_message_id")),
            sequence=int(data["sequence"]) if data.get("sequence") is not None else None,
            metadata=dict(_mapping_of(data.get("metadata"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(slots=True)
class InboundEvent:
    kind: EventKind
    conversation: ConversationRef
    sender: ParticipantRef | None = None
    message_id: str | None = None
    content: str | None = None
    content_type: ContentKind = "text"
    raw_content: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    reply_grant: ReplyGrant | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> InboundEvent:
        kind = str(data.get("kind") or "message")
        if kind not in {"message", "interaction", "reaction", "read_receipt", "lifecycle"}:
            kind = "message"
        sender = data.get("sender")
        reply_grant = data.get("reply_grant")
        attachments = data.get("attachments") or []
        return cls(
            kind=kind,  # type: ignore[arg-type]
            conversation=ConversationRef.from_mapping(
                _mapping_of(data.get("conversation")),
                default_platform=str(data.get("platform") or "unknown"),
                default_chat_id=str(data.get("chat_id") or "default"),
            ),
            sender=sender if isinstance(sender, ParticipantRef) else ParticipantRef.from_mapping(_mapping_of(sender))
            if sender is not None
            else None,
            message_id=_as_str(data.get("message_id")),
            content=_as_str(data.get("content")),
            content_type=str(data.get("content_type") or "text"),  # type: ignore[arg-type]
            raw_content=_as_str(data.get("raw_content")),
            attachments=[
                item if isinstance(item, Attachment) else Attachment.from_mapping(_mapping_of(item)) for item in attachments
            ],
            reply_grant=reply_grant
            if isinstance(reply_grant, ReplyGrant)
            else ReplyGrant.from_mapping(_mapping_of(reply_grant))
            if reply_grant is not None
            else None,
            metadata=dict(_mapping_of(data.get("metadata"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)


@dataclass(slots=True)
class OutboundAction:
    kind: ActionKind
    conversation: ConversationRef | None = None
    text: str | None = None
    content_type: ContentKind = "text"
    message_id: str | None = None
    reply_to_message_id: str | None = None
    reply_grant: ReplyGrant | None = None
    attachments: list[Attachment] = field(default_factory=list)
    live_surface: LiveSurfaceRef | None = None
    reaction: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        data: Mapping[str, Any],
        *,
        default_conversation: ConversationRef | None = None,
    ) -> OutboundAction:
        kind = str(data.get("kind") or "send_message")
        if kind not in {
            "send_message",
            "reply_message",
            "edit_message",
            "patch_message",
            "update_card",
            "stream_card",
            "append_follow_up",
            "delete_message",
            "set_reaction",
            "pin_message",
            "mark_read",
            "presence",
            "escalate_message",
            "custom",
        }:
            kind = "custom"
        conversation_value = data.get("conversation")
        reply_grant = data.get("reply_grant")
        live_surface = data.get("live_surface")
        attachments = data.get("attachments") or []
        return cls(
            kind=kind,  # type: ignore[arg-type]
            conversation=conversation_value
            if isinstance(conversation_value, ConversationRef)
            else ConversationRef.from_mapping(_mapping_of(conversation_value))
            if conversation_value is not None
            else default_conversation,
            text=_as_str(data.get("text") or data.get("content")),
            content_type=str(data.get("content_type") or "text"),  # type: ignore[arg-type]
            message_id=_as_str(data.get("message_id")),
            reply_to_message_id=_as_str(data.get("reply_to_message_id")),
            reply_grant=reply_grant
            if isinstance(reply_grant, ReplyGrant)
            else ReplyGrant.from_mapping(_mapping_of(reply_grant))
            if reply_grant is not None
            else None,
            attachments=[
                item if isinstance(item, Attachment) else Attachment.from_mapping(_mapping_of(item)) for item in attachments
            ],
            live_surface=live_surface
            if isinstance(live_surface, LiveSurfaceRef)
            else LiveSurfaceRef.from_mapping(_mapping_of(live_surface))
            if live_surface is not None
            else None,
            reaction=_as_str(data.get("reaction")),
            metadata=dict(_mapping_of(data.get("metadata"))),
        )

    def as_dict(self) -> dict[str, Any]:
        return to_primitive(self)
