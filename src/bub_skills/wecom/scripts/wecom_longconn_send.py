#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bub.channels.wecom_longconn_bot import WeComLongConnBotChannel
from bub.social import Attachment, ConversationRef, MentionTarget, OutboundAction, ReplyGrant


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a WeCom long-connection message using Bub's native bridge adapter.")
    parser.add_argument("--chat-id", required=True, help="Target WeCom user_id or group chatid")
    parser.add_argument(
        "--kind",
        default="send_message",
        choices=["send_message", "reply_message", "update_card"],
        help="Outbound action kind",
    )
    parser.add_argument(
        "--content-type",
        default="text",
        choices=["text", "rich_text", "card", "image", "file", "audio"],
        help="Outbound content type",
    )
    parser.add_argument("--message", help="Message text content")
    parser.add_argument("--card-json", help="Template card JSON object")
    parser.add_argument("--attachment", help="Attachment path/URL/data URL for image/file/audio sends")
    parser.add_argument("--reply-token", help="WeCom callback req_id for reply/update flows")
    parser.add_argument("--reply-to-message-id", help="Optional source message id")
    parser.add_argument("--event-type", help="Optional callback event type, such as enter_chat or template_card_event")
    parser.add_argument("--target-id", action="append", default=[], help="Target user id list for update_card")
    parser.add_argument("--mention-user", action="append", default=[], help="Mention a WeCom user_id")
    parser.add_argument("--mention-mobile", action="append", default=[], help="Mention a mobile number")
    parser.add_argument("--mention-all", action="store_true", help="Mention @all")
    return parser.parse_args()


def _card_from_json(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("--card-json must decode to a JSON object")
    return data


def _mentions(args: argparse.Namespace) -> list[MentionTarget]:
    items = [MentionTarget(kind="user_id", value=value) for value in args.mention_user]
    items.extend(MentionTarget(kind="mobile", value=value) for value in args.mention_mobile)
    if args.mention_all:
        items.append(MentionTarget(kind="all", value="@all"))
    return items


def _attachments(args: argparse.Namespace) -> list[Attachment]:
    if not args.attachment:
        return []
    return [Attachment(content_type=_attachment_content_type(args.content_type), url=args.attachment)]


def _attachment_content_type(content_type: str) -> str:
    if content_type == "image":
        return "image/*"
    if content_type == "audio":
        return "audio/*"
    return "application/octet-stream"


def _reply_grant(args: argparse.Namespace) -> ReplyGrant | None:
    if not args.reply_token and not args.reply_to_message_id and not args.event_type:
        return None
    metadata: dict[str, object] = {}
    if args.event_type:
        metadata["event_type"] = args.event_type
    return ReplyGrant(
        mode="token" if args.reply_token else "message_id",
        token=args.reply_token,
        reply_to_message_id=args.reply_to_message_id,
        metadata=metadata,
    )


async def _main_async(args: argparse.Namespace) -> None:
    async def _on_receive(_message) -> None:
        return

    channel = WeComLongConnBotChannel(on_receive=_on_receive)
    stop_event = asyncio.Event()
    action = OutboundAction(
        kind=args.kind,
        conversation=ConversationRef(platform="wecom", route_channel="wecom_longconn_bot", chat_id=args.chat_id),
        text=args.message,
        content_type=args.content_type,
        card=_card_from_json(args.card_json),
        reply_grant=_reply_grant(args),
        reply_to_message_id=args.reply_to_message_id,
        attachments=_attachments(args),
        mentions=_mentions(args),
        target_ids=[str(item) for item in args.target_id],
    )

    await channel.start(stop_event)
    try:
        await channel.send(action)
    finally:
        await channel.stop()


def main() -> int:
    args = _parse_args()
    try:
        asyncio.run(_main_async(args))
    except Exception as exc:
        print(f"wecom long-connection send failed: {exc}", file=sys.stderr)
        return 1
    print("wecom long-connection action sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
