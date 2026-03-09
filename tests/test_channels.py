from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bub.channels.cli import CliChannel
from bub.channels.handler import BufferedMessageHandler
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.channels.telegram import BubMessageFilter, TelegramChannel
from bub.channels.wecom_longconn_bot import WeComLongConnBotChannel
from bub.channels.wecom_webhook import WeComWebhookChannel
from bub.social import ConversationRef, OutboundAction, ReplyGrant


class FakeChannel:
    def __init__(self, name: str, *, needs_debounce: bool = False) -> None:
        self.name = name
        self._needs_debounce = needs_debounce
        self.sent: list[OutboundAction] = []
        self.started = False
        self.stopped = False

    @property
    def needs_debounce(self) -> bool:
        return self._needs_debounce

    async def start(self, stop_event: asyncio.Event) -> None:
        self.started = True
        self.stop_event = stop_event

    async def stop(self) -> None:
        self.stopped = True

    async def send(self, action: OutboundAction) -> None:
        self.sent.append(action)


class FakeFramework:
    def __init__(self, channels: dict[str, FakeChannel]) -> None:
        self._channels = channels
        self.router = None

    def get_channels(self, message_handler):
        self.message_handler = message_handler
        return self._channels

    def bind_outbound_router(self, router) -> None:
        self.router = router


def _message(
    content: str,
    *,
    channel: str = "telegram",
    session_id: str = "telegram:chat",
    chat_id: str = "chat",
    is_active: bool = False,
    kind: str = "normal",
) -> ChannelMessage:
    return ChannelMessage(
        session_id=session_id,
        channel=channel,
        chat_id=chat_id,
        content=content,
        is_active=is_active,
        kind=kind,
    )


@pytest.mark.asyncio
async def test_buffered_handler_passes_commands_through_immediately() -> None:
    handled: list[str] = []

    async def receive(message: ChannelMessage) -> None:
        handled.append(message.content)

    handler = BufferedMessageHandler(
        receive,
        active_time_window=10,
        max_wait_seconds=10,
        debounce_seconds=0.01,
    )

    await handler(_message(",help"))

    assert handled == [",help"]


def test_buffered_handler_prettify_masks_base64_payload() -> None:
    content = 'look data:image/png;base64,abcdef" end'

    assert BufferedMessageHandler.prettify(content) == 'look [media]" end'


def test_buffered_handler_prettify_masks_single_quoted_base64_payload() -> None:
    content = "look 'data:image/png;base64,abcdef' end"

    assert BufferedMessageHandler.prettify(content) == "look '[media]' end"


def test_buffered_handler_prettify_preserves_following_text_for_unquoted_payload() -> None:
    content = "look DATA:image/png;base64,abcdef tail"

    assert BufferedMessageHandler.prettify(content) == "look [media] tail"


@pytest.mark.asyncio
async def test_channel_manager_dispatch_uses_output_channel_and_preserves_metadata() -> None:
    cli_channel = FakeChannel("cli")
    manager = ChannelManager(FakeFramework({"cli": cli_channel}), enabled_channels=["cli"])

    result = await manager.dispatch(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="cli", chat_id="room", account_id="default"),
            text="hello",
            metadata={"message_kind": "command", "source": "test"},
        )
    )

    assert result is True
    assert len(cli_channel.sent) == 1
    action = cli_channel.sent[0]
    assert action.kind == "send_message"
    assert action.text == "hello"
    assert action.metadata["message_kind"] == "command"
    assert action.metadata["source"] == "test"
    assert action.conversation == ConversationRef(platform="cli", chat_id="room", account_id="default")


@pytest.mark.asyncio
async def test_channel_manager_dispatch_prefers_conversation_route_channel() -> None:
    webhook_channel = FakeChannel("wecom_webhook")
    manager = ChannelManager(FakeFramework({"wecom_webhook": webhook_channel}), enabled_channels=["wecom_webhook"])

    result = await manager.dispatch(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(
                platform="wecom",
                route_channel="wecom_webhook",
                chat_id="room",
                adapter_mode="webhook_sink",
                transport="webhook",
            ),
            text="hello",
        )
    )

    assert result is True
    assert webhook_channel.sent[0].conversation.channel_key == "wecom_webhook"


def test_channel_manager_enabled_channels_excludes_cli_from_all() -> None:
    channels = {"cli": FakeChannel("cli"), "telegram": FakeChannel("telegram"), "discord": FakeChannel("discord")}
    manager = ChannelManager(FakeFramework(channels), enabled_channels=["all"])

    assert [channel.name for channel in manager.enabled_channels()] == ["telegram", "discord"]


@pytest.mark.asyncio
async def test_channel_manager_on_receive_uses_buffer_for_debounced_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    telegram = FakeChannel("telegram", needs_debounce=True)
    manager = ChannelManager(FakeFramework({"telegram": telegram}), enabled_channels=["telegram"])
    calls: list[ChannelMessage] = []

    class StubBufferedMessageHandler:
        def __init__(
            self, handler, *, active_time_window: float, max_wait_seconds: float, debounce_seconds: float
        ) -> None:
            self.handler = handler
            self.settings = (active_time_window, max_wait_seconds, debounce_seconds)

        async def __call__(self, message: ChannelMessage) -> None:
            calls.append(message)

    import bub.channels.manager as manager_module

    monkeypatch.setattr(manager_module, "BufferedMessageHandler", StubBufferedMessageHandler)

    message = _message("hello", channel="telegram")
    await manager.on_receive(message)
    await manager.on_receive(message)

    assert calls == [message, message]
    assert message.session_id in manager._session_handlers
    assert isinstance(manager._session_handlers[message.session_id], StubBufferedMessageHandler)


@pytest.mark.asyncio
async def test_channel_manager_shutdown_cancels_tasks_and_stops_enabled_channels() -> None:
    telegram = FakeChannel("telegram")
    cli = FakeChannel("cli")
    manager = ChannelManager(FakeFramework({"telegram": telegram, "cli": cli}), enabled_channels=["all"])

    async def never_finish() -> None:
        await asyncio.sleep(10)

    task = asyncio.create_task(never_finish())
    manager._ongoing_tasks.add(task)

    await manager.shutdown()

    assert task.cancelled()
    assert telegram.stopped is True
    assert cli.stopped is False


def test_cli_channel_normalize_input_prefixes_shell_commands() -> None:
    channel = CliChannel.__new__(CliChannel)
    channel._mode = "shell"

    assert channel._normalize_input("ls") == ",ls"
    assert channel._normalize_input(",help") == ",help"


@pytest.mark.asyncio
async def test_cli_channel_send_routes_by_message_kind() -> None:
    channel = CliChannel.__new__(CliChannel)
    events: list[tuple[str, str]] = []
    channel._renderer = SimpleNamespace(
        error=lambda content: events.append(("error", content)),
        command_output=lambda content: events.append(("command", content)),
        assistant_output=lambda content: events.append(("assistant", content)),
    )

    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="cli", chat_id="local"),
            text="bad",
            metadata={"message_kind": "error"},
        )
    )
    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="cli", chat_id="local"),
            text="ok",
            metadata={"message_kind": "command"},
        )
    )
    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="cli", chat_id="local"),
            text="hi",
        )
    )

    assert events == [("error", "bad"), ("command", "ok"), ("assistant", "hi")]


def test_cli_channel_history_file_uses_workspace_hash(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"

    result = CliChannel._history_file(home, workspace)

    assert result.parent == home / "history"
    assert result.suffix == ".history"


def test_bub_message_filter_accepts_private_messages() -> None:
    message = SimpleNamespace(chat=SimpleNamespace(type="private"), text="hello")

    assert BubMessageFilter().filter(message) is True


def test_bub_message_filter_requires_group_mention_or_reply() -> None:
    bot = SimpleNamespace(id=1, username="BubBot")
    message = SimpleNamespace(
        chat=SimpleNamespace(type="group"),
        text="hello team",
        caption=None,
        entities=[],
        caption_entities=[],
        reply_to_message=None,
        get_bot=lambda: bot,
    )

    assert BubMessageFilter().filter(message) is False


def test_bub_message_filter_accepts_group_mention() -> None:
    bot = SimpleNamespace(id=1, username="BubBot")
    message = SimpleNamespace(
        chat=SimpleNamespace(type="group"),
        text="ping @bubbot",
        caption=None,
        entities=[SimpleNamespace(type="mention", offset=5, length=7)],
        caption_entities=[],
        reply_to_message=None,
        get_bot=lambda: bot,
    )

    assert BubMessageFilter().filter(message) is True


@pytest.mark.asyncio
async def test_telegram_channel_send_extracts_json_message_and_skips_blank() -> None:
    channel = TelegramChannel(lambda message: None)
    sent: list[tuple[str, str]] = []

    async def send_message(chat_id: str, text: str) -> None:
        sent.append((chat_id, text))

    channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))

    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="telegram", chat_id="42"),
            text="hello",
        )
    )
    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="telegram", chat_id="42"),
            text="   ",
        )
    )

    assert sent == [("42", "hello")]


@pytest.mark.asyncio
async def test_telegram_channel_build_message_returns_command_directly() -> None:
    channel = TelegramChannel(lambda message: None)
    channel._parser = SimpleNamespace(parse=_async_return((",help", {"type": "text"})), get_reply=_async_return(None))

    message = SimpleNamespace(
        chat_id=42,
        message_id=7,
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=8, full_name="Alice", username="alice", is_bot=False),
    )

    result = await channel._build_message(message)

    assert result.channel == "telegram"
    assert result.chat_id == "42"
    assert result.content == ",help"
    assert result.output_channel == "telegram"
    assert result.message_id == "7"
    assert result.reply_grant == ReplyGrant(mode="message_id", reply_to_message_id="7")


@pytest.mark.asyncio
async def test_telegram_channel_build_message_wraps_payload_and_disables_outbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = TelegramChannel(lambda message: None)
    parser = SimpleNamespace(
        parse=_async_return(("hello", {"type": "text", "sender_id": "7"})),
        get_reply=_async_return({"message": "prev", "type": "text"}),
    )
    channel._parser = parser
    monkeypatch.setattr("bub.channels.telegram.MESSAGE_FILTER.filter", lambda message: True)

    message = SimpleNamespace(
        chat_id=42,
        message_id=7,
        chat=SimpleNamespace(type="group"),
        from_user=SimpleNamespace(id=8, full_name="Alice", username="alice", is_bot=False),
    )

    result = await channel._build_message(message)

    assert result.output_channel == "null"
    assert result.is_active is True
    assert '"message": "hello"' in result.content
    assert '"chat_id": "42"' in result.content
    assert '"reply_to_message"' in result.content
    assert result.lifespan is not None
    assert result.conversation == ConversationRef(platform="telegram", chat_id="42", account_id="default", surface="group")
    assert result.reply_grant == ReplyGrant(mode="message_id", reply_to_message_id="7")


@pytest.mark.asyncio
async def test_telegram_channel_send_supports_reply_and_edit_actions() -> None:
    channel = TelegramChannel(lambda message: None)
    events: list[tuple[str, dict[str, object]]] = []

    async def send_message(**kwargs) -> None:
        events.append(("send", kwargs))

    async def edit_message_text(**kwargs) -> None:
        events.append(("edit", kwargs))

    channel._app = SimpleNamespace(bot=SimpleNamespace(send_message=send_message, edit_message_text=edit_message_text))

    await channel.send(
        OutboundAction(
            kind="send_message",
            text="ignored",
            conversation=ConversationRef(platform="telegram", chat_id="42", surface="direct"),
        )
    )
    await channel.send(
        OutboundAction(
            kind="reply_message",
            text="hello",
            reply_to_message_id="5",
            conversation=ConversationRef(platform="telegram", chat_id="42", surface="direct"),
        )
    )
    await channel.send(
        OutboundAction(
            kind="edit_message",
            text="updated",
            message_id="9",
            conversation=ConversationRef(platform="telegram", chat_id="42", surface="direct"),
        )
    )

    assert events == [
        ("send", {"chat_id": "42", "text": "ignored"}),
        ("send", {"chat_id": "42", "text": "hello", "reply_to_message_id": 5}),
        ("edit", {"chat_id": "42", "message_id": 9, "text": "updated"}),
    ]


@pytest.mark.asyncio
async def test_wecom_webhook_channel_send_text_with_mentions(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = WeComWebhookChannel()
    channel._settings.webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
    sent: list[dict[str, object]] = []

    async def post_json(payload: dict[str, object]) -> None:
        sent.append(payload)

    monkeypatch.setattr(channel, "_post_json", post_json)

    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="wecom", route_channel="wecom_webhook", chat_id="room"),
            text="hello",
            mentions=[
                {"kind": "user_id", "value": "zhangsan"},
                {"kind": "mobile", "value": "13800001111"},
                {"kind": "all", "value": "@all"},
            ],
        )
    )

    assert sent == [
        {
            "msgtype": "text",
            "text": {
                "content": "hello",
                "mentioned_list": ["zhangsan", "@all"],
                "mentioned_mobile_list": ["13800001111", "@all"],
            },
        }
    ]


@pytest.mark.asyncio
async def test_wecom_webhook_channel_send_file_uploads_media_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    channel = WeComWebhookChannel()
    channel._settings.webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
    sent: list[dict[str, object]] = []
    file_path = tmp_path / "report.pdf"

    async def upload_media(media_type: str, action: OutboundAction) -> str:
        assert media_type == "file"
        assert action.text == str(file_path)
        return "MEDIA123"

    async def post_json(payload: dict[str, object]) -> None:
        sent.append(payload)

    monkeypatch.setattr(channel, "_upload_media", upload_media)
    monkeypatch.setattr(channel, "_post_json", post_json)

    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="wecom", route_channel="wecom_webhook", chat_id="room"),
            text=str(file_path),
            content_type="file",
        )
    )

    assert sent == [{"msgtype": "file", "file": {"media_id": "MEDIA123"}}]


def test_wecom_webhook_channel_upload_url_derives_key_and_type() -> None:
    channel = WeComWebhookChannel()
    channel._settings.webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"

    assert (
        channel._upload_url("voice")
        == "https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key=test-key&type=voice"
    )


@pytest.mark.asyncio
async def test_wecom_longconn_bot_channel_start_and_send_via_bridge() -> None:
    received: list[ChannelMessage] = []

    async def on_receive(message: ChannelMessage) -> None:
        received.append(message)

    channel = WeComLongConnBotChannel(on_receive=on_receive)
    channel._settings.command = (
        f"{sys.executable} -m bub.channels.dev_bridge "
        "--channel wecom_longconn_bot --chat-id chat-1 --boot-message 'hello from bridge' --echo-actions"
    )

    await channel.start(asyncio.Event())
    assert channel.is_ready is True
    assert channel.bridge_info["channel"] == "wecom_longconn_bot"
    await asyncio.sleep(0.2)
    await channel.send(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(
                platform="wecom",
                route_channel="wecom_longconn_bot",
                chat_id="chat-1",
                adapter_mode="bridge",
                transport="long_connection",
            ),
            text="ping",
        )
    )
    await asyncio.sleep(0.2)
    await channel.stop()

    assert channel.is_ready is False
    assert received[0].channel == "wecom_longconn_bot"
    assert received[0].content == "hello from bridge"
    assert received[1].content == "echo: ping"


def test_wecom_longconn_bot_channel_capabilities_and_command_parsing() -> None:
    channel = WeComLongConnBotChannel(lambda message: None)
    channel._settings = channel._settings.model_copy(
        update={
            "command": "python bridge.py --flag",
            "bot_id": "bot-id",
            "secret": "token-value",
            "pairing_code": "PAIR-123",
        }
    )

    assert list(channel.command) == ["python", "bridge.py", "--flag"]
    assert channel.capabilities.transport == "long_connection"
    assert channel.capabilities.adapter_mode == "bridge"
    assert channel.capabilities.provisioning.mode == "interactive_pairing"
    assert channel.capabilities.provisioning.pairing_code == "PAIR-123"
    assert channel.ready_timeout_seconds == 5.0


def _async_return(value):
    async def runner(*args, **kwargs):
        return value

    return runner
