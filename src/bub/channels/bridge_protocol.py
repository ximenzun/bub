from __future__ import annotations

from typing import Any

from bub.channels.message import ChannelMessage
from bub.social import OutboundAction, to_primitive

BRIDGE_PROTOCOL_VERSION = "1"


def build_action_frame(action: OutboundAction) -> dict[str, Any]:
    return {"type": "action", "version": BRIDGE_PROTOCOL_VERSION, "action": to_primitive(action)}


def build_ready_frame(channel: str, **metadata: Any) -> dict[str, Any]:
    return {"type": "ready", "version": BRIDGE_PROTOCOL_VERSION, "channel": channel, **metadata}


def build_log_frame(message: str, *, level: str = "info", **metadata: Any) -> dict[str, Any]:
    return {"type": "log", "version": BRIDGE_PROTOCOL_VERSION, "level": level, "message": message, **metadata}


def build_inbound_message_frame(message: ChannelMessage) -> dict[str, Any]:
    return {"type": "message", "version": BRIDGE_PROTOCOL_VERSION, "message": to_primitive(message)}
