"""Compatibility helpers for bridging envelopes into structured social actions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from bub.envelope import content_of, field_of
from bub.social.types import Attachment, ConversationRef, InboundEvent, OutboundAction, ReplyGrant, normalize_surface
from bub.types import Envelope


def conversation_of(
    message: Envelope,
    *,
    default_platform: str | None = None,
    default_chat_id: str = "default",
) -> ConversationRef | None:
    raw = field_of(message, "conversation")
    if isinstance(raw, ConversationRef):
        return raw
    if isinstance(raw, Mapping):
        return ConversationRef.from_mapping(raw, default_platform=default_platform, default_chat_id=default_chat_id)

    platform = field_of(message, "channel", default_platform)
    chat_id = field_of(message, "chat_id", default_chat_id)
    if platform is None and chat_id is None:
        return None
    surface = normalize_surface(field_of(message, "surface", field_of(message, "chat_type", "unknown")))
    metadata = field_of(message, "conversation_metadata", {}) or {}
    return ConversationRef(
        platform=str(platform or default_platform or "unknown"),
        chat_id=str(chat_id or default_chat_id),
        account_id=str(field_of(message, "account_id", "default")),
        surface=surface,
        thread_id=_string_or_none(field_of(message, "thread_id")),
        lane_id=_string_or_none(field_of(message, "lane_id")),
        actor_id=_string_or_none(field_of(message, "actor_id")),
        tenant_id=_string_or_none(field_of(message, "tenant_id")),
        metadata=dict(metadata) if isinstance(metadata, Mapping) else {},
    )


def sender_of(message: Envelope) -> Any:
    return field_of(message, "sender")


def reply_grant_of(message: Envelope) -> ReplyGrant | None:
    raw = field_of(message, "reply_grant")
    if isinstance(raw, ReplyGrant):
        return raw
    if isinstance(raw, Mapping):
        return ReplyGrant.from_mapping(raw)
    reply_to_message_id = field_of(message, "reply_to_message_id")
    if reply_to_message_id is None:
        return None
    return ReplyGrant(mode="message_id", reply_to_message_id=str(reply_to_message_id))


def attachments_of(message: Envelope) -> list[Attachment]:
    raw = field_of(message, "attachments", []) or []
    attachments: list[Attachment] = []
    if not isinstance(raw, list | tuple):
        return attachments
    for item in raw:
        if isinstance(item, Attachment):
            attachments.append(item)
        elif isinstance(item, Mapping):
            attachments.append(Attachment.from_mapping(item))
    return attachments


def outbound_actions_of(message: Envelope, *, default_platform: str | None = None) -> list[OutboundAction]:
    default_conversation = conversation_of(message, default_platform=default_platform)
    raw_actions = field_of(message, "actions")
    if isinstance(raw_actions, list | tuple) and raw_actions:
        actions: list[OutboundAction] = []
        for item in raw_actions:
            if isinstance(item, OutboundAction):
                actions.append(item)
            elif isinstance(item, Mapping):
                actions.append(OutboundAction.from_mapping(item, default_conversation=default_conversation))
        return actions

    text = content_of(message)
    attachments = attachments_of(message)
    reply_grant = reply_grant_of(message)
    reply_to_message_id = _string_or_none(field_of(message, "reply_to_message_id"))
    if not text.strip() and not attachments:
        return []
    kind = "reply_message" if reply_to_message_id or (reply_grant and reply_grant.reply_to_message_id) else "send_message"
    return [
        OutboundAction(
            kind=kind,  # type: ignore[arg-type]
            conversation=default_conversation,
            text=text,
            content_type=str(field_of(message, "content_type", "text")),  # type: ignore[arg-type]
            reply_to_message_id=reply_to_message_id,
            reply_grant=reply_grant,
            attachments=attachments,
            metadata=dict(field_of(message, "metadata", {}) or {}),
        )
    ]


def inbound_event_of(message: Envelope) -> InboundEvent:
    conversation = conversation_of(message)
    if conversation is None:
        raise ValueError("message does not contain enough information to build a ConversationRef")
    raw_event = field_of(message, "social_event")
    if isinstance(raw_event, InboundEvent):
        return raw_event
    if isinstance(raw_event, Mapping):
        return InboundEvent.from_mapping(raw_event)
    return InboundEvent(
        kind=str(field_of(message, "event_kind", "message")),  # type: ignore[arg-type]
        conversation=conversation,
        sender=field_of(message, "sender"),
        message_id=_string_or_none(field_of(message, "message_id")),
        content=content_of(message),
        content_type=str(field_of(message, "content_type", "text")),  # type: ignore[arg-type]
        raw_content=_string_or_none(field_of(message, "raw_content")),
        attachments=attachments_of(message),
        reply_grant=reply_grant_of(message),
        metadata=dict(field_of(message, "metadata", {}) or {}),
    )


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
