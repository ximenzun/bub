from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

from bub.channels.bridge_protocol import (
    build_inbound_message_frame,
    build_log_frame,
    build_provisioning_frame,
    build_ready_frame,
    build_state_frame,
)
from bub.channels.message import ChannelMessage
from bub.social import MentionTarget, OutboundAction, ProvisioningInfo


@dataclass(slots=True)
class _BridgeState:
    channel: str
    chat_id: str
    configured: bool = False
    config: dict[str, Any] | None = None


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Bundled WeCom long-connection bridge for Bub.")
    parser.add_argument("--channel", default="wecom_longconn_bot")
    parser.add_argument("--chat-id", default="wecom-dev-chat")
    parser.add_argument("--boot-message", default="")
    parser.add_argument("--echo-actions", action="store_true")
    args = parser.parse_args()

    state = _BridgeState(channel=args.channel, chat_id=args.chat_id)
    if args.boot_message:
        _emit(
            build_inbound_message_frame(
                ChannelMessage(
                    session_id=f"{args.channel}:{args.chat_id}",
                    channel=args.channel,
                    chat_id=args.chat_id,
                    content=args.boot_message,
                    is_active=True,
                )
            )
        )

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            _emit(build_log_frame("invalid json from host", level="warning", raw=raw))
            continue

        frame_type = str(frame.get("type", ""))
        if frame_type == "configure":
            await _handle_configure(frame, state)
            continue
        if frame_type == "action":
            await _handle_action(frame, state, echo_actions=args.echo_actions)
            continue
        _emit(build_log_frame("ignored frame", level="debug", frame_type=frame_type))

    return 0


async def _handle_configure(frame: dict[str, Any], state: _BridgeState) -> None:
    config = frame.get("config", {})
    state.config = config if isinstance(config, dict) else {}
    state.configured = bool(state.config.get("bot_id") and state.config.get("secret"))
    provisioning = ProvisioningInfo(
        mode="interactive_pairing",
        state="active" if state.configured else "pending",
        pairing_code=_string_or_none(state.config.get("pairing_code")),
        config_key=_string_or_none(state.config.get("config_key")),
        metadata={
            "callback_token": _string_or_none(state.config.get("callback_token")),
            "encoding_aes_key": _string_or_none(state.config.get("encoding_aes_key")),
        },
    )
    _emit(build_provisioning_frame(provisioning))
    _emit(build_state_frame("configured", configured=state.configured))
    _emit(build_ready_frame(state.channel, name="wecom_longconn_bridge", configured=state.configured))


async def _handle_action(frame: dict[str, Any], state: _BridgeState, *, echo_actions: bool) -> None:
    action_payload = frame.get("action", {})
    if not isinstance(action_payload, dict):
        _emit(build_log_frame("invalid action payload", level="warning"))
        return
    action = OutboundAction.from_mapping(action_payload)
    request = translate_action_to_wecom_request(action)
    _emit(build_log_frame("translated action", request=request))
    if echo_actions and action.text:
        _emit(
            build_inbound_message_frame(
                ChannelMessage(
                    session_id=f"{state.channel}:{state.chat_id}",
                    channel=state.channel,
                    chat_id=state.chat_id,
                    content=f"echo: {action.text}",
                )
            )
        )


def translate_action_to_wecom_request(action: OutboundAction) -> dict[str, Any]:
    msgtype = infer_wecom_msgtype(action)
    mode = "passive_reply" if action.reply_to_message_id or action.reply_grant else "proactive_reply"
    request: dict[str, Any] = {
        "mode": mode,
        "msgtype": msgtype,
        "conversation": action.conversation.as_dict() if action.conversation is not None else None,
    }
    if msgtype == "text":
        request["payload"] = build_text_payload(action)
        return request
    if msgtype in {"markdown", "markdown_v2"}:
        request["payload"] = {msgtype: {"content": action.text or ""}}
        return request
    if msgtype == "template_card":
        request["payload"] = {"template_card": action.metadata.get("template_card", {})}
        return request
    if msgtype == "event_ack":
        request["payload"] = {"event_id": action.metadata.get("event_id")}
        return request
    request["payload"] = {"content": action.text or "", "metadata": action.metadata}
    return request


def infer_wecom_msgtype(action: OutboundAction) -> str:
    if action.metadata.get("wecom_msgtype"):
        return str(action.metadata["wecom_msgtype"])
    if action.content_type == "card":
        return "template_card"
    if action.content_type == "rich_text":
        return "markdown"
    return "text"


def build_text_payload(action: OutboundAction) -> dict[str, Any]:
    mentioned_list: list[str] = []
    mentioned_mobile_list: list[str] = []
    for mention in action.mentions:
        if not isinstance(mention, MentionTarget):
            continue
        if mention.kind == "user_id":
            mentioned_list.append(mention.value)
        elif mention.kind == "mobile":
            mentioned_mobile_list.append(mention.value)
        elif mention.kind == "all":
            mentioned_list.append("@all")
            mentioned_mobile_list.append("@all")
    payload: dict[str, Any] = {"content": action.text or ""}
    if mentioned_list:
        payload["mentioned_list"] = mentioned_list
    if mentioned_mobile_list:
        payload["mentioned_mobile_list"] = mentioned_mobile_list
    return {"text": payload}


def _emit(frame: dict[str, object]) -> None:
    print(json.dumps(frame, ensure_ascii=False), flush=True)


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
