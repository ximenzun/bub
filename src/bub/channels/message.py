from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from bub.social.types import (
    Attachment,
    ConversationRef,
    OutboundAction,
    ParticipantRef,
    ReplyGrant,
    normalize_surface,
    to_primitive,
)

type MessageKind = Literal["error", "normal", "command"]


@dataclass
class ChannelMessage:
    """Structured message data from channels to framework."""

    session_id: str
    channel: str
    content: str
    chat_id: str = "default"
    is_active: bool = False
    kind: MessageKind = "normal"
    context: dict[str, Any] = field(default_factory=dict)
    lifespan: contextlib.AbstractAsyncContextManager | None = None
    output_channel: str = ""
    account_id: str = "default"
    message_id: str | None = None
    conversation: ConversationRef | None = None
    sender: ParticipantRef | None = None
    reply_grant: ReplyGrant | None = None
    attachments: list[Attachment] = field(default_factory=list)
    actions: list[OutboundAction] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.conversation, dict):
            self.conversation = ConversationRef.from_mapping(
                self.conversation,
                default_platform=self.channel,
                default_chat_id=self.chat_id,
            )
        if isinstance(self.sender, dict):
            self.sender = ParticipantRef.from_mapping(self.sender)
        if isinstance(self.reply_grant, dict):
            self.reply_grant = ReplyGrant.from_mapping(self.reply_grant)
        self.attachments = [
            item if isinstance(item, Attachment) else Attachment.from_mapping(item) for item in self.attachments
        ]
        self.actions = [
            item
            if isinstance(item, OutboundAction)
            else OutboundAction.from_mapping(item, default_conversation=self.conversation)
            for item in self.actions
        ]
        if self.conversation is None:
            self.conversation = ConversationRef(
                platform=self.channel,
                chat_id=self.chat_id,
                account_id=self.account_id,
                surface=normalize_surface(self.context.get("surface", self.context.get("chat_type", "unknown"))),
                thread_id=str(self.context["thread_id"]) if "thread_id" in self.context else None,
            )
        self.context.update({"channel": "$" + self.channel, "chat_id": self.chat_id})
        if self.account_id != "default":
            self.context.setdefault("account_id", self.account_id)
        if self.message_id is not None:
            self.context.setdefault("message_id", self.message_id)
        if not self.output_channel:  # output to the same channel by default
            self.output_channel = self.channel

    @property
    def context_str(self) -> str:
        """String representation of the context for prompt building."""
        return json.dumps(to_primitive(self.context), ensure_ascii=False)[1:-1]

    @classmethod
    def from_batch(cls, batch: list[ChannelMessage]) -> ChannelMessage:
        """Create a single message by combining a batch of messages."""
        if not batch:
            raise ValueError("Batch cannot be empty")
        template = batch[-1]
        content = "\n".join(message.content for message in batch)
        return replace(template, content=content)
