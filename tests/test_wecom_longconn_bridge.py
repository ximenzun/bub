from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
from pathlib import Path

import pytest

from bub.channels.bridge_protocol import build_action_frame, build_configure_frame
from bub.social import Attachment, ConversationRef, OutboundAction, ReplyGrant

BRIDGE_SCRIPT = Path(__file__).resolve().parents[1] / "src/bub/channels/node/wecom_longconn_bridge.mjs"


async def _write_frame(process: asyncio.subprocess.Process, frame: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write((json.dumps(frame) + "\n").encode("utf-8"))
    await process.stdin.drain()


async def _read_until_log(process: asyncio.subprocess.Process) -> dict[str, object]:
    assert process.stdout is not None
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=3)
        if not line:
            raise RuntimeError("bridge closed before producing a translation log")
        record = json.loads(line.decode("utf-8"))
        if record.get("type") == "log" and record.get("message") in {"translated action", "failed to translate action"}:
            return record


async def _translate_action(action: OutboundAction) -> dict[str, object]:
    process = await asyncio.create_subprocess_exec(
        "node",
        str(BRIDGE_SCRIPT),
        "--channel",
        "wecom_longconn_bot",
        "--mock",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await _write_frame(
            process,
            build_configure_frame("wecom_longconn_bot", {"bot_id": "bot-id", "secret": "secret"}),
        )
        await _write_frame(process, build_action_frame(action))
        return await _read_until_log(process)
    finally:
        if process.stdin is not None:
            process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.wait_closed()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await process.wait()


@pytest.mark.asyncio
async def test_node_wecom_longconn_bridge_translates_proactive_text_to_markdown_send() -> None:
    record = await _translate_action(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="wecom", route_channel="wecom_longconn_bot", chat_id="chat-1"),
            text="hello",
        )
    )

    request = record["request"]
    assert request["op"] == "sendMessage"
    assert request["args"] == ["chat-1", {"msgtype": "markdown", "markdown": {"content": "hello"}}]


@pytest.mark.asyncio
async def test_node_wecom_longconn_bridge_translates_passive_image_reply() -> None:
    image_bytes = b"hello"
    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    request_id = "req-1"

    record = await _translate_action(
        OutboundAction(
            kind="reply_message",
            conversation=ConversationRef(platform="wecom", route_channel="wecom_longconn_bot", chat_id="chat-1"),
            text="caption",
            content_type="image",
            attachments=[Attachment(content_type="image/png", url=f"data:image/png;base64,{image_base64}")],
            reply_grant=ReplyGrant(mode="token", token=request_id),
        )
    )

    request = record["request"]
    assert request["op"] == "replyStream"
    assert request["args"][0] == {"headers": {"req_id": request_id}}
    assert request["args"][2] == "caption"
    assert request["args"][4] == [
        {
            "msgtype": "image",
            "image": {
                "base64": image_base64,
                "md5": hashlib.md5(image_bytes, usedforsecurity=False).hexdigest(),
            },
        }
    ]


@pytest.mark.asyncio
async def test_node_wecom_longconn_bridge_translates_passive_text_reply_to_reply_stream() -> None:
    request_id = "req-1"

    record = await _translate_action(
        OutboundAction(
            kind="reply_message",
            conversation=ConversationRef(platform="wecom", route_channel="wecom_longconn_bot", chat_id="chat-1"),
            text="hello",
            reply_grant=ReplyGrant(mode="token", token=request_id),
        )
    )

    request = record["request"]
    assert request["op"] == "replyStream"
    assert request["args"][0] == {"headers": {"req_id": request_id}}
    assert isinstance(request["args"][1], str)
    assert request["args"][2:] == ["hello", True]


@pytest.mark.asyncio
async def test_node_wecom_longconn_bridge_translates_update_card_from_native_fields() -> None:
    card = {"card_type": "text_notice", "main_title": {"title": "updated"}}
    request_id = "req-1"

    record = await _translate_action(
        OutboundAction(
            kind="update_card",
            conversation=ConversationRef(platform="wecom", route_channel="wecom_longconn_bot", chat_id="chat-1"),
            content_type="card",
            card=card,
            target_ids=["zhangsan", "lisi"],
            reply_grant=ReplyGrant(mode="token", token=request_id, metadata={"event_type": "template_card_event"}),
        )
    )

    request = record["request"]
    assert request["op"] == "updateTemplateCard"
    assert request["args"] == [{"headers": {"req_id": request_id}}, card, ["zhangsan", "lisi"]]
