from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from bub.channels.cli import CliChannel
from bub.channels.handler import BufferedMessageHandler
from bub.channels.manager import ChannelManager
from bub.channels.message import ChannelMessage
from bub.channels.telegram import BubMessageFilter, TelegramChannel, TelegramMessageParser
from bub.social import Attachment, ConversationRef, LiveSurfaceRef, MentionTarget, OutboundAction, ReplyGrant


class FakeChannel:
    def __init__(self, name: str, *, needs_debounce: bool = False) -> None:
        self.name = name
        self._needs_debounce = needs_debounce
        self.sent: list[ChannelMessage] = []
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

    async def send(self, message: ChannelMessage) -> None:
        self.sent.append(message)


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


@pytest.mark.asyncio
async def test_channel_manager_dispatch_uses_output_channel_and_preserves_metadata() -> None:
    cli_channel = FakeChannel("cli")
    manager = ChannelManager(FakeFramework({"cli": cli_channel}), enabled_channels=["cli"])

    result = await manager.dispatch({
        "session_id": "session",
        "channel": "telegram",
        "output_channel": "cli",
        "chat_id": "room",
        "content": "hello",
        "kind": "command",
        "context": {"source": "test"},
    })

    assert result is True
    assert len(cli_channel.sent) == 1
    outbound = cli_channel.sent[0]
    assert outbound.channel == "cli"
    assert outbound.chat_id == "room"
    assert outbound.content == "hello"
    assert outbound.kind == "command"
    assert outbound.context["source"] == "test"


@pytest.mark.asyncio
async def test_channel_manager_dispatch_preserves_structured_fields_from_mapping() -> None:
    lark = FakeChannel("lark")
    manager = ChannelManager(FakeFramework({"lark": lark}), enabled_channels=["lark"])
    attachment_path = "/var/bub/demo.png"

    result = await manager.dispatch(
        {
            "session_id": "session",
            "channel": "cli",
            "output_channel": "lark",
            "chat_id": "oc_123",
            "content": "see attachment",
            "message_id": "om_456",
            "attachments": [
                {
                    "content_type": "image/png",
                    "metadata": {"path": attachment_path},
                }
            ],
            "reply_grant": {"mode": "message_id", "reply_to_message_id": "om_parent"},
            "conversation": {"platform": "lark", "chat_id": "oc_123", "thread_id": "omt_789"},
            "metadata": {"source": "test"},
        }
    )

    assert result is True
    assert len(lark.sent) == 1
    outbound = lark.sent[0]
    assert outbound.channel == "lark"
    assert outbound.output_channel == "lark"
    assert outbound.message_id == "om_456"
    assert outbound.attachments == [Attachment(content_type="image/png", metadata={"path": attachment_path})]
    assert outbound.reply_grant == ReplyGrant(mode="message_id", reply_to_message_id="om_parent")
    assert outbound.conversation == ConversationRef(platform="lark", chat_id="oc_123", thread_id="omt_789")
    assert outbound.metadata == {"source": "test"}


@pytest.mark.asyncio
async def test_channel_manager_dispatch_mapping_preserves_structured_attachments_and_reply_grant() -> None:
    cli_channel = FakeChannel("cli")
    manager = ChannelManager(FakeFramework({"cli": cli_channel}), enabled_channels=["cli"])
    reply_token = "-".join(["req", "1"])

    result = await manager.dispatch(
        {
            "session_id": "session",
            "channel": "telegram",
            "output_channel": "cli",
            "chat_id": "room",
            "content": "hello",
            "attachments": [
                {
                    "content_type": "image/png",
                    "url": "file:///tmp/example.png",
                }
            ],
            "reply_grant": {"mode": "token", "token": reply_token},
            "metadata": {"source": "test"},
        }
    )

    assert result is True
    outbound = cli_channel.sent[0]
    assert outbound.attachments == [Attachment(content_type="image/png", url="file:///tmp/example.png")]
    assert outbound.reply_grant == ReplyGrant(mode="token", token=reply_token)
    assert outbound.metadata == {"source": "test"}


@pytest.mark.asyncio
async def test_channel_manager_dispatch_preserves_channel_message_context_and_account_id() -> None:
    telegram = FakeChannel("telegram")
    manager = ChannelManager(FakeFramework({"telegram": telegram}), enabled_channels=["telegram"])
    outbound = ChannelMessage(
        session_id="telegram:room:15",
        channel="telegram",
        chat_id="room",
        content="hello",
        account_id="acct-1",
        output_channel="telegram",
        context={"reply_to_message_id": "42", "thread_id": "15"},
    )

    result = await manager.dispatch(outbound)

    assert result is True
    assert telegram.sent == [outbound]


@pytest.mark.asyncio
async def test_channel_manager_dispatch_converts_outbound_action_for_telegram() -> None:
    telegram = FakeChannel("telegram")
    manager = ChannelManager(FakeFramework({"telegram": telegram}), enabled_channels=["telegram"])

    result = await manager.dispatch(
        OutboundAction(
            kind="set_draft",
            conversation=ConversationRef(platform="telegram", chat_id="room", thread_id="12"),
            text="working",
            live_surface=LiveSurfaceRef(mode="text_draft", surface_id="77", parent_message_id="42"),
            reply_grant=ReplyGrant(mode="message_id", reply_to_message_id="42"),
        )
    )

    assert result is True
    assert len(telegram.sent) == 1
    outbound = telegram.sent[0]
    assert outbound.channel == "telegram"
    assert outbound.chat_id == "room"
    assert outbound.content == "working"
    assert outbound.context["telegram_kind"] == "set_draft"
    assert outbound.context["surface_id"] == "77"
    assert outbound.context["reply_to_message_id"] == "42"


@pytest.mark.asyncio
async def test_channel_manager_dispatch_converts_outbound_action_for_lark_with_path_attachment() -> None:
    lark = FakeChannel("lark")
    manager = ChannelManager(FakeFramework({"lark": lark}), enabled_channels=["lark"])
    attachment_path = "/var/bub/demo.png"

    result = await manager.dispatch(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="lark", chat_id="oc_123"),
            text="see image",
            content_type="image",
            attachments=[Attachment(content_type="image/png", metadata={"path": attachment_path})],
        )
    )

    assert result is True
    assert len(lark.sent) == 1
    outbound = lark.sent[0]
    assert outbound.channel == "lark"
    assert outbound.content == "see image"
    assert outbound.context["content_type"] == "image"
    assert outbound.context["attachment"] == attachment_path
    assert outbound.attachments == [Attachment(content_type="image/png", metadata={"path": attachment_path})]


@pytest.mark.asyncio
async def test_channel_manager_dispatch_converts_outbound_action_for_lark_with_url_attachment() -> None:
    lark = FakeChannel("lark")
    manager = ChannelManager(FakeFramework({"lark": lark}), enabled_channels=["lark"])
    attachment = Attachment(content_type="image/png", url="file:///tmp/chart.png")

    result = await manager.dispatch(
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="lark", chat_id="room"),
            text="see chart",
            content_type="image",
            attachments=[attachment],
        )
    )

    assert result is True
    outbound = lark.sent[0]
    assert outbound.attachments == [attachment]
    assert outbound.context["content_type"] == "image"
    assert outbound.context["attachment"] == "file:///tmp/chart.png"


@pytest.mark.asyncio
async def test_channel_manager_dispatch_converts_outbound_action_for_wecom_with_structured_fields() -> None:
    wecom = FakeChannel("wecom_longconn_bot")
    manager = ChannelManager(FakeFramework({"wecom_longconn_bot": wecom}), enabled_channels=["wecom_longconn_bot"])
    attachment = Attachment(content_type="image/png", url="file:///tmp/chart.png")
    reply_token = "-".join(["req", "1"])
    reply_grant = ReplyGrant(mode="token", token=reply_token)

    result = await manager.dispatch(
        OutboundAction(
            kind="reply_message",
            conversation=ConversationRef(platform="wecom", route_channel="wecom_longconn_bot", chat_id="room"),
            text="see chart",
            content_type="image",
            attachments=[attachment],
            mentions=[MentionTarget(kind="user_id", value="user-1")],
            target_ids=["user-2"],
            reply_grant=reply_grant,
        )
    )

    assert result is True
    outbound = wecom.sent[0]
    assert outbound.content == "see chart"
    assert outbound.attachments == [attachment]
    assert outbound.reply_grant == reply_grant
    assert outbound.context["wecom_kind"] == "reply_message"
    assert outbound.context["content_type"] == "image"
    assert outbound.context["wecom_reply_token"] == reply_token
    assert outbound.metadata["mentions"] == [{"kind": "user_id", "value": "user-1", "label": None}]
    assert outbound.metadata["target_ids"] == ["user-2"]


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

    await channel.send(_message("bad", channel="cli", kind="error"))
    await channel.send(_message("ok", channel="cli", kind="command"))
    await channel.send(_message("hi", channel="cli"))

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

    await channel.send(_message('{"message":"hello"}', chat_id="42"))
    await channel.send(_message("   ", chat_id="42"))

    assert sent == [("42", "hello")]


@pytest.mark.asyncio
async def test_telegram_channel_send_supports_reply_edit_draft_and_presence() -> None:
    channel = TelegramChannel(lambda message: None, slash_commands=[("/repo", "Repo help")])
    events: list[tuple[str, Any]] = []

    async def send_message(chat_id: str, text: str, **kwargs: Any) -> None:
        events.append(("send", (chat_id, text, kwargs)))

    async def edit_message_text(chat_id: str, message_id: int, text: str) -> None:
        events.append(("edit", (chat_id, message_id, text)))

    async def send_message_draft(chat_id: str, draft_id: int, text: str, message_thread_id: int | None = None) -> None:
        events.append(("draft", (chat_id, draft_id, text, message_thread_id)))

    async def send_chat_action(chat_id: str, action: str) -> None:
        events.append(("presence", (chat_id, action)))

    async def set_my_commands(commands: list[Any]) -> None:
        events.append(("commands", commands))

    channel._app = SimpleNamespace(
        bot=SimpleNamespace(
            send_message=send_message,
            edit_message_text=edit_message_text,
            send_message_draft=send_message_draft,
            send_chat_action=send_chat_action,
            set_my_commands=set_my_commands,
        )
    )

    await channel._set_registered_commands()
    await channel.send(_message("hello", chat_id="42", kind="normal"))
    await channel.send(
        ChannelMessage(
            session_id="telegram:42",
            channel="telegram",
            chat_id="42",
            content="reply",
            context={"telegram_kind": "reply_message", "reply_to_message_id": "9"},
        )
    )
    await channel.send(
        ChannelMessage(
            session_id="telegram:42",
            channel="telegram",
            chat_id="42",
            content="updated",
            context={"telegram_kind": "edit_message", "message_id": "11"},
        )
    )
    await channel.send(
        ChannelMessage(
            session_id="telegram:42",
            channel="telegram",
            chat_id="42",
            content="working",
            context={"telegram_kind": "set_draft", "surface_id": "77", "thread_id": "15"},
        )
    )
    await channel.send(
        ChannelMessage(
            session_id="telegram:42",
            channel="telegram",
            chat_id="42",
            content="",
            context={"telegram_kind": "presence"},
        )
    )

    assert events[0][0] == "commands"
    assert events[1] == ("send", ("42", "hello", {}))
    assert events[2] == ("send", ("42", "reply", {"reply_to_message_id": 9}))
    assert events[3] == ("edit", ("42", 11, "updated"))
    assert events[4] == ("draft", ("42", 77, "working", 15))
    assert events[5] == ("presence", ("42", "typing"))


@pytest.mark.asyncio
async def test_telegram_channel_build_message_returns_command_directly() -> None:
    channel = TelegramChannel(lambda message: None)
    channel._parser = SimpleNamespace(parse=_async_return((",help", {"type": "text"})), get_reply=_async_return(None))

    message = SimpleNamespace(chat_id=42, message_id=9, chat=SimpleNamespace(type="private"), from_user=None)

    result = await channel._build_message(message)

    assert result.channel == "telegram"
    assert result.chat_id == "42"
    assert result.content == ",help"
    assert result.output_channel == "telegram"
    assert result.context["reply_to_message_id"] == "9"


@pytest.mark.asyncio
async def test_telegram_channel_build_message_wraps_payload_and_enables_native_outbound(
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
        message_id=11,
        chat=SimpleNamespace(type="group"),
        from_user=SimpleNamespace(id=7),
        message_thread_id=15,
    )

    result = await channel._build_message(message)

    assert result.output_channel == "telegram"
    assert result.session_id == "telegram:42:15"
    assert result.is_active is True
    assert '"message": "hello"' in result.content
    assert '"reply_to_message"' in result.content
    assert result.lifespan is not None
    assert result.context["reply_to_message_id"] == "11"
    assert result.context["thread_id"] == "15"
    assert result.context["actor_id"] == "7"


@pytest.mark.asyncio
async def test_telegram_message_parser_extracts_formatted_links() -> None:
    parser = TelegramMessageParser()
    message = SimpleNamespace(
        text="Docs and https://example.com",
        caption=None,
        entities=[
            SimpleNamespace(type="text_link", url="https://docs.example.com"),
            SimpleNamespace(type="url", offset=9, length=19),
        ],
        caption_entities=[],
        message_id=1,
        from_user=SimpleNamespace(username="alice", full_name="Alice", id=7, is_bot=False),
        date=datetime(2026, 3, 11),
    )

    content, metadata = await parser.parse(message)

    assert content == "Docs and https://example.com"
    assert metadata["links"] == ["https://docs.example.com", "https://example.com"]


@pytest.mark.asyncio
async def test_telegram_message_parser_extracts_links_from_caption_entities() -> None:
    parser = TelegramMessageParser()
    message = SimpleNamespace(
        text=None,
        caption="See portal",
        entities=[],
        caption_entities=[SimpleNamespace(type="text_link", url="https://portal.example.com")],
        message_id=2,
        from_user=SimpleNamespace(username="alice", full_name="Alice", id=7, is_bot=False),
        date=datetime(2026, 3, 11),
        photo=[SimpleNamespace(file_id="file-1", file_size=3, width=1, height=1)],
    )

    async def fake_download_media(file_id: str, file_size: int) -> bytes:
        assert file_id == "file-1"
        assert file_size == 3
        return b"img"

    parser._download_media = fake_download_media  # type: ignore[method-assign]

    _content, metadata = await parser.parse(message)

    assert metadata["links"] == ["https://portal.example.com"]


def _async_return(value):
    async def runner(*args, **kwargs):
        return value

    return runner
