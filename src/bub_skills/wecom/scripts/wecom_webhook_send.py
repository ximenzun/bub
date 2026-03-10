#!/usr/bin/env python3

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from bub.channels.wecom_webhook import WeComWebhookChannel
from bub.social import Attachment, ConversationRef, MentionTarget, OutboundAction


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a WeCom webhook message using Bub's native webhook adapter.")
    parser.add_argument("--message", help="Message text content")
    parser.add_argument(
        "--content-type",
        default="text",
        choices=["text", "rich_text", "card", "image", "file", "audio"],
        help="Outbound content type",
    )
    parser.add_argument("--card-json", help="Template card JSON object")
    parser.add_argument("--attachment", help="Attachment path/URL/data URL for image/file/audio sends")
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


async def _main_async(args: argparse.Namespace) -> None:
    channel = WeComWebhookChannel()
    action = OutboundAction(
        kind="send_message",
        conversation=ConversationRef(platform="wecom", route_channel="wecom_webhook", chat_id="wecom-webhook"),
        text=args.message,
        content_type=args.content_type,
        card=_card_from_json(args.card_json),
        attachments=_attachments(args),
        mentions=_mentions(args),
    )
    await channel.send(action)


def main() -> int:
    args = _parse_args()
    try:
        asyncio.run(_main_async(args))
    except Exception as exc:
        print(f"wecom webhook send failed: {exc}", file=sys.stderr)
        return 1
    print("wecom webhook message sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
