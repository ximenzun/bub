"""Social-platform abstractions for rich channel adapters."""

from bub.social.capabilities import ActionConstraint, ChannelCapabilities, basic_channel_capabilities
from bub.social.types import (
    ActionKind,
    Attachment,
    ContentKind,
    ConversationRef,
    ConversationSurface,
    EventKind,
    InboundEvent,
    LiveSurfaceRef,
    OutboundAction,
    ParticipantRef,
    ProgressSurface,
    ReplyGrant,
    ReplyMode,
    normalize_surface,
    to_primitive,
)

__all__ = [
    "ActionConstraint",
    "ActionKind",
    "Attachment",
    "ChannelCapabilities",
    "ContentKind",
    "ConversationRef",
    "ConversationSurface",
    "EventKind",
    "InboundEvent",
    "LiveSurfaceRef",
    "OutboundAction",
    "ParticipantRef",
    "ProgressSurface",
    "ReplyGrant",
    "ReplyMode",
    "basic_channel_capabilities",
    "normalize_surface",
    "to_primitive",
]
