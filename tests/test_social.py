from __future__ import annotations

from bub.channels.message import ChannelMessage
from bub.social import Attachment, ConversationRef, OutboundAction, ReplyGrant
from bub.social.compat import attachments_of, inbound_event_of, outbound_actions_of


def test_channel_message_infers_conversation_from_channel_and_chat_id() -> None:
    message = ChannelMessage(session_id="s", channel="telegram", chat_id="42", content="hello")

    assert message.conversation == ConversationRef(platform="telegram", chat_id="42", account_id="default")


def test_outbound_actions_of_defaults_to_reply_message_when_reply_grant_present() -> None:
    actions = outbound_actions_of(
        {
            "channel": "telegram",
            "chat_id": "42",
            "content": "hello",
            "reply_grant": {"mode": "message_id", "reply_to_message_id": "9"},
        }
    )

    assert actions == [
        OutboundAction(
            kind="reply_message",
            conversation=ConversationRef(platform="telegram", chat_id="42", account_id="default"),
            text="hello",
            reply_grant=ReplyGrant(mode="message_id", reply_to_message_id="9"),
        )
    ]


def test_attachments_of_coerces_mapping_payloads() -> None:
    attachments = attachments_of(
        {
            "attachments": [
                {
                    "content_type": "image/png",
                    "url": "https://example.test/image.png",
                    "file_key": "abc",
                }
            ]
        }
    )

    assert attachments == [Attachment(content_type="image/png", url="https://example.test/image.png", file_key="abc")]


def test_inbound_event_of_uses_richer_channel_message_metadata() -> None:
    message = ChannelMessage(
        session_id="session",
        channel="telegram",
        chat_id="42",
        content="hello",
        message_id="99",
        reply_grant=ReplyGrant(mode="message_id", reply_to_message_id="99"),
    )

    event = inbound_event_of(message)

    assert event.message_id == "99"
    assert event.reply_grant == ReplyGrant(mode="message_id", reply_to_message_id="99")
    assert event.conversation == ConversationRef(platform="telegram", chat_id="42", account_id="default")
