from __future__ import annotations

from bub.channels.message import ChannelMessage
from bub.social import (
    Attachment,
    ChannelCapabilities,
    ContentConstraint,
    ConversationRef,
    CredentialSpec,
    MentionTarget,
    OutboundAction,
    ProvisioningInfo,
    ReplyGrant,
)
from bub.social.compat import attachments_of, inbound_event_of, outbound_actions_of

FAKE_TARGET_USER_ID = "wecom_user_8f12ab34"


def test_channel_message_infers_conversation_from_channel_and_chat_id() -> None:
    message = ChannelMessage(session_id="s", channel="telegram", chat_id="42", content="hello")

    assert message.conversation == ConversationRef(platform="telegram", chat_id="42", account_id="default")
    assert message.context_str == ""


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


def test_outbound_action_from_mapping_preserves_native_card_fields() -> None:
    request_id = "req-1"
    action = OutboundAction.from_mapping(
        {
            "kind": "update_card",
            "conversation": {"platform": "wecom", "chat_id": "chat-1"},
            "content_type": "card",
            "card": {"card_type": "text_notice", "main_title": {"title": "hello"}},
            "target_ids": [FAKE_TARGET_USER_ID],
            "reply_grant": {"mode": "token", "token": request_id},
        }
    )

    assert action.kind == "update_card"
    assert action.card == {"card_type": "text_notice", "main_title": {"title": "hello"}}
    assert action.target_ids == [FAKE_TARGET_USER_ID]
    assert action.reply_grant == ReplyGrant(mode="token", token=request_id)


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


def test_conversation_ref_supports_route_channel_for_multi_adapter_platforms() -> None:
    conversation = ConversationRef(
        platform="wecom",
        route_channel="wecom_longconn_bot",
        chat_id="chat-1",
        adapter_mode="session_bot",
        transport="long_connection",
    )

    assert conversation.channel_key == "wecom_longconn_bot"
    assert conversation.as_dict()["adapter_mode"] == "session_bot"
    assert conversation.as_dict()["transport"] == "long_connection"


def test_provisioning_and_credentials_cover_wecom_long_connection_bot_shape() -> None:
    provisioning = ProvisioningInfo(
        mode="interactive_pairing",
        state="pending",
        pairing_code="PAIR-123",
        config_key="CONFIG-456",
    )
    capabilities = ChannelCapabilities(
        platform="wecom",
        adapter_mode="bridge",
        transport="long_connection",
        provisioning_mode="interactive_pairing",
        credential_specs=(
            CredentialSpec(key="bot_id", kind="bot_secret", secret=False, env_var="WECOM_BOT_ID"),
            CredentialSpec(key="secret", kind="bot_secret", env_var="WECOM_BOT_SECRET"),
        ),
        provisioning=provisioning,
        content_constraints={
            "text": ContentConstraint(max_body_bytes=2048, supports_mentions=True),
            "card": ContentConstraint(notes=("template_card",)),
        },
    )

    assert capabilities.provisioning.mode == "interactive_pairing"
    assert capabilities.provisioning.state == "pending"
    assert capabilities.credential_specs[0].key == "bot_id"
    assert capabilities.content_constraints["text"].max_body_bytes == 2048
    assert capabilities.content_constraints["card"].notes == ("template_card",)


def test_mention_target_supports_wecom_mobile_mentions() -> None:
    mention = MentionTarget(kind="mobile", value="13800001111", label="oncall")

    assert mention.as_dict() == {"kind": "mobile", "value": "13800001111", "label": "oncall"}
