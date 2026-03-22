from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bub.builtin.hook_impl as hook_impl_module
from bub.builtin.hook_impl import AGENTS_FILE_NAME, DEFAULT_SYSTEM_PROMPT, BuiltinImpl
from bub.builtin.resource_refs import RESOURCE_REFS_KEY
from bub.builtin.store import FileTapeStore
from bub.channels.message import ChannelMessage, MediaItem
from bub.framework import BubFramework
from bub.social import Attachment, ConversationRef, ReplyGrant


class RecordingLifespan:
    def __init__(self) -> None:
        self.entered = False
        self.exit_args: tuple[object, object, object] | None = None

    async def __aenter__(self) -> None:
        self.entered = True

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exit_args = (exc_type, exc, traceback)


class FakeAgent:
    def __init__(self, home: Path) -> None:
        self.settings = SimpleNamespace(home=home)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def run(self, *, session_id: str, prompt: str, state: dict[str, object]) -> str:
        self.calls.append((session_id, prompt, state))
        return "agent-output"


def _raise_value_error() -> None:
    raise ValueError("boom")


def _build_impl(tmp_path: Path) -> tuple[BubFramework, BuiltinImpl, FakeAgent]:
    framework = BubFramework()
    impl = BuiltinImpl(framework)
    agent = FakeAgent(tmp_path)
    impl.agent = agent  # type: ignore[assignment]
    return framework, impl, agent


def test_resolve_session_prefers_explicit_session_id(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    message = ChannelMessage(session_id="  keep-me  ", channel="cli", chat_id="room", content="hello")

    assert impl.resolve_session(message) == "  keep-me  "


def test_resolve_session_falls_back_to_channel_and_chat_id(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    message = {"session_id": "   ", "channel": "telegram", "chat_id": "42", "content": "hello"}

    assert impl.resolve_session(message) == "telegram:42"


@pytest.mark.asyncio
async def test_load_state_and_save_state_manage_lifespan_and_context(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    lifespan = RecordingLifespan()
    message = ChannelMessage(
        session_id="session",
        channel="cli",
        chat_id="room",
        content="hello",
        lifespan=lifespan,
        context={"message_id": "m-1", "thread_id": "t-1", "actor_id": "user-1"},
    )

    state = await impl.load_state(message=message, session_id="resolved-session")

    assert lifespan.entered is True
    assert state["session_id"] == "resolved-session"
    assert state["_runtime_agent"] is impl.agent
    assert "context" not in state
    assert state["_inbound_channel"] == "cli"
    assert state["_inbound_chat_id"] == "room"
    assert state["_inbound_message_id"] == "m-1"
    assert state["_inbound_thread_id"] == "t-1"
    assert state["_inbound_actor_id"] == "user-1"

    try:
        _raise_value_error()
    except ValueError as exc:
        await impl.save_state(
            session_id="resolved-session",
            state=state,
            message=message,
            model_output="ignored",
        )
        assert isinstance(exc, ValueError)

    assert lifespan.exit_args is not None
    assert lifespan.exit_args[0] is ValueError
    assert isinstance(lifespan.exit_args[1], ValueError)


@pytest.mark.asyncio
async def test_build_prompt_marks_commands_and_prefixes_context(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    command = ChannelMessage(session_id="s", channel="cli", chat_id="room", content=",help")
    normal = ChannelMessage(session_id="s", channel="cli", chat_id="room", content="hello")

    command_prompt = await impl.build_prompt(command, session_id="s", state={})
    normal_prompt = await impl.build_prompt(normal, session_id="s", state={})

    assert command_prompt == ",help"
    assert command.kind == "command"
    assert normal_prompt == "hello"


@pytest.mark.asyncio
async def test_build_prompt_restores_recent_image_when_user_references_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, impl, _ = _build_impl(tmp_path)
    message = ChannelMessage(session_id="s", channel="lark", chat_id="room", content="查看上面图片内容")
    restored = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}}]
    recent_refs = [
        {
            "kind": "image",
            "scope": "message",
            "content_type": "image/*",
            "locator": {
                "kind": "channel_file",
                "channel": "lark",
                "message_id": "om_1",
                "file_key": "img_1",
                "resource_type": "image",
            },
        }
    ]

    async def fake_recent(*args, **kwargs):
        return recent_refs

    async def fake_parts(refs):
        assert refs == recent_refs
        return restored

    monkeypatch.setattr(hook_impl_module, "_recent_image_refs", fake_recent)
    monkeypatch.setattr(hook_impl_module, "_image_parts_from_refs", fake_parts)

    state: dict[str, object] = {}
    prompt = await impl.build_prompt(message, session_id="s", state=state)

    assert prompt == [{"type": "text", "text": "查看上面图片内容"}, *restored]
    assert state["_inbound_media_parts"] == restored
    assert state["_inbound_resource_refs"] == recent_refs
    assert state["_inbound_media_refs"] == [
        {
            "channel": "lark",
            "message_id": "om_1",
            "file_key": "img_1",
            "resource_type": "image",
            "content_type": "image/*",
        }
    ]


@pytest.mark.asyncio
async def test_build_prompt_includes_quoted_message_metadata(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="wecom_longconn_bot",
        chat_id="room",
        content="帮我看一下这条引用",
        metadata={
            "quoted_message": {
                "channel": "wecom_longconn_bot",
                "text": "这是被引用的图片",
                "attachments": [
                    {
                        "content_type": "image/png",
                        "url": "data:image/png;base64,cG5n",
                        "metadata": {"bub_scope": "quote"},
                    }
                ],
            }
        },
    )

    prompt = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(prompt, list)
    assert prompt[0]["text"] == "Quoted message:\n这是被引用的图片"
    assert prompt[1]["image_url"]["url"] == "data:image/png;base64,cG5n"
    assert "Current message:" in prompt[2]["text"]


@pytest.mark.asyncio
async def test_build_prompt_persists_non_image_attachment_resource_refs(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"RIFF0000")
    message = ChannelMessage(
        session_id="s",
        channel="wechat_clawbot",
        chat_id="room",
        content="[audio]",
        attachments=[Attachment(content_type="audio/wav", metadata={"path": str(audio_path)})],
    )

    state: dict[str, object] = {}
    prompt = await impl.build_prompt(message, session_id="s", state=state)

    assert prompt == "[audio]"
    assert state["_inbound_resource_refs"] == [
        {
            "kind": "audio",
            "scope": "message",
            "content_type": "audio/wav",
            "locator": {"kind": "path", "path": str(audio_path)},
        }
    ]
    assert state["_inbound_media_refs"] == [
        {
            "channel": "unknown",
            "url": str(audio_path),
            "content_type": "audio/wav",
        }
    ]


@pytest.mark.asyncio
async def test_build_prompt_restores_lark_reply_target_from_tape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, impl, agent = _build_impl(tmp_path)
    restored = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,cG5n"}}]

    async def fake_image_parts(refs):
        assert refs == [
            {
                "kind": "image",
                "scope": "message",
                "content_type": "image/*",
                "locator": {
                    "kind": "channel_file",
                    "channel": "lark",
                    "message_id": "om_parent",
                    "file_key": "img_parent",
                    "resource_type": "image",
                },
            }
        ]
        return restored

    tape = SimpleNamespace(
        query_async=SimpleNamespace(
            all=AsyncMock(
                return_value=[
                    SimpleNamespace(kind="anchor", payload={"name": "session/start"}),
                    SimpleNamespace(
                        kind="message",
                        payload={
                            "role": "user",
                            "_bub_inbound_message_id": "om_parent",
                            "content": "[Lark image]",
                            RESOURCE_REFS_KEY: [
                                {
                                    "kind": "image",
                                    "scope": "message",
                                    "content_type": "image/*",
                                    "locator": {
                                        "kind": "channel_file",
                                        "channel": "lark",
                                        "message_id": "om_parent",
                                        "file_key": "img_parent",
                                        "resource_type": "image",
                                    },
                                }
                            ],
                        },
                    ),
                ]
            )
        )
    )

    agent.tapes = SimpleNamespace(session_tape=lambda session_id, workspace: tape)  # type: ignore[assignment]
    monkeypatch.setattr(hook_impl_module, "_image_parts_from_refs", fake_image_parts)

    message = ChannelMessage(
        session_id="s",
        channel="lark",
        chat_id="room",
        content="看这条reply引用的图片",
        context={"parent_id": "om_parent"},
    )
    state = {"_lark_parent_id": "om_parent", "_runtime_workspace": str(tmp_path)}

    prompt = await impl.build_prompt(message, session_id="s", state=state)

    assert isinstance(prompt, list)
    assert prompt[0]["text"] == "Quoted message:\n[Lark image]"
    assert prompt[1]["image_url"]["url"] == "data:image/png;base64,cG5n"


@pytest.mark.asyncio
async def test_image_parts_from_refs_ignores_non_image_path_refs(tmp_path: Path) -> None:
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(b"RIFF0000")

    parts = await hook_impl_module._image_parts_from_refs(
        [
            {
                "kind": "audio",
                "scope": "message",
                "content_type": "audio/wav",
                "locator": {"kind": "path", "path": str(audio_path)},
            }
        ]
    )

    assert parts == []


@pytest.mark.asyncio
async def test_build_prompt_adds_guidance_for_image_only_message(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    message = ChannelMessage(
        session_id="s",
        channel="lark",
        chat_id="room",
        content="[Lark image]",
        media=[MediaItem(type="image", mime_type="image/png", data_fetcher=_async_return(b"png"))],
    )

    prompt = await impl.build_prompt(message, session_id="s", state={})

    assert isinstance(prompt, list)
    assert "Describe the image briefly" in prompt[0]["text"]


@pytest.mark.asyncio
async def test_run_model_delegates_to_agent(tmp_path: Path) -> None:
    _, impl, agent = _build_impl(tmp_path)
    state = {"context": "ctx"}

    result = await impl.run_model(prompt="prompt", session_id="session", state=state)

    assert result == "agent-output"
    assert agent.calls == [("session", "prompt", state)]


def test_system_prompt_appends_workspace_agents_file(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    (tmp_path / AGENTS_FILE_NAME).write_text("local rules", encoding="utf-8")

    result = impl.system_prompt(prompt="hello", state={"_runtime_workspace": str(tmp_path)})

    assert result == DEFAULT_SYSTEM_PROMPT + "\n\nlocal rules"


def test_system_prompt_ignores_missing_agents_file(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    result = impl.system_prompt(prompt="hello", state={"_runtime_workspace": str(tmp_path)})

    assert result == DEFAULT_SYSTEM_PROMPT + "\n\n"


def test_provide_channels_returns_cli_and_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, impl, agent = _build_impl(tmp_path)

    class DummyCliChannel:
        name = "cli"

        def __init__(self, on_receive, agent) -> None:
            self.on_receive = on_receive
            self.agent = agent

    class DummyTelegramChannel:
        name = "telegram"

        def __init__(self, on_receive) -> None:
            self.on_receive = on_receive

    import bub.channels.cli
    import bub.channels.telegram

    monkeypatch.setattr(bub.channels.cli, "CliChannel", DummyCliChannel)
    monkeypatch.setattr(bub.channels.telegram, "TelegramChannel", DummyTelegramChannel)

    def message_handler(message) -> None:
        return None

    channels = impl.provide_channels(message_handler)

    assert [channel.name for channel in channels] == ["telegram", "cli"]
    assert channels[0].on_receive is message_handler
    assert channels[1].on_receive is message_handler
    assert channels[1].agent is agent


@pytest.mark.asyncio
async def test_on_error_dispatches_outbound_message(tmp_path: Path) -> None:
    framework, impl, _ = _build_impl(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    async def call_many(name: str, **kwargs: object) -> list[object]:
        calls.append((name, kwargs))
        return []

    framework._hook_runtime.call_many = call_many  # type: ignore[method-assign]

    await impl.on_error(stage="turn", error=RuntimeError("bad"), message={"channel": "cli", "chat_id": "room"})

    assert len(calls) == 1
    hook_name, kwargs = calls[0]
    outbound = kwargs["message"]
    assert hook_name == "dispatch_outbound"
    assert outbound.channel == "cli"
    assert outbound.chat_id == "room"
    assert outbound.kind == "error"
    assert outbound.content == "An error occurred at stage 'turn': bad"


@pytest.mark.asyncio
async def test_on_error_hides_low_level_responses_shape_error_from_chat_user(tmp_path: Path) -> None:
    framework, impl, _ = _build_impl(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    async def call_many(name: str, **kwargs: object) -> list[object]:
        calls.append((name, kwargs))
        return []

    framework._hook_runtime.call_many = call_many  # type: ignore[method-assign]

    await impl.on_error(
        stage="turn",
        error=RuntimeError("openai:gpt-5.2: Responses API returned an unexpected type: <class 'str'>"),
        message={"channel": "lark", "chat_id": "room", "content": "重试并截图给我"},
    )

    outbound = calls[0][1]["message"]
    assert outbound.kind == "error"
    assert "模型响应解析阶段出了内部错误" in outbound.content
    assert "Responses API returned an unexpected type" not in outbound.content


@pytest.mark.asyncio
async def test_dispatch_outbound_uses_framework_router(tmp_path: Path) -> None:
    framework, impl, _ = _build_impl(tmp_path)
    dispatched: list[object] = []

    async def dispatch_via_router(message: object) -> bool:
        dispatched.append(message)
        return True

    framework.dispatch_via_router = dispatch_via_router  # type: ignore[method-assign]
    outbound = {"session_id": "session", "channel": "cli", "chat_id": "room", "content": "hello"}

    result = await impl.dispatch_outbound(outbound)

    assert result is True
    assert dispatched == [outbound]


def test_render_outbound_preserves_message_metadata(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    reply_token = "-".join(["reply", "grant", "1"])
    conversation = ConversationRef(platform="telegram", chat_id="room", account_id="acct-1", surface="direct")
    reply_grant = ReplyGrant(mode="token", token=reply_token, reply_to_message_id="77")

    rendered = impl.render_outbound(
        message={
            "channel": "telegram",
            "chat_id": "room",
            "kind": "command",
            "output_channel": "cli",
            "context": {
                "message_id": "77",
                "thread_id": "15",
                "account_id": "acct-1",
                "wecom_reply_token": reply_token,
            },
            "conversation": conversation,
            "reply_grant": reply_grant,
            "attachments": [{"content_type": "image/png", "url": "file:///tmp/inbound.png"}],
            "metadata": {"origin": "test"},
        },
        session_id="session",
        state={},
        model_output="result",
    )

    assert len(rendered) == 1
    outbound = rendered[0]
    assert outbound.session_id == "session"
    assert outbound.channel == "cli"
    assert outbound.chat_id == "room"
    assert outbound.output_channel == "cli"
    assert outbound.kind == "command"
    assert outbound.content == "result"
    assert outbound.account_id == "acct-1"
    assert outbound.context["reply_to_message_id"] == "77"
    assert outbound.context["thread_id"] == "15"
    assert outbound.context["wecom_reply_token"] == reply_token
    assert outbound.conversation == conversation
    assert outbound.reply_grant == reply_grant
    assert outbound.attachments == []
    assert outbound.metadata == {"origin": "test"}


def test_provide_tape_store_uses_agent_home_directory(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)

    store = impl.provide_tape_store()

    assert isinstance(store, FileTapeStore)
    assert store._directory == tmp_path / "tapes"


def _async_return(value):
    async def runner(*args, **kwargs):
        return value

    return runner


def test_render_outbound_returns_empty_when_suppressed(tmp_path: Path) -> None:
    _, impl, _ = _build_impl(tmp_path)
    message = ChannelMessage(session_id="s", channel="cli", chat_id="room", content="hello")

    rendered = impl.render_outbound(
        message=message, session_id="s", state={"_suppress_default_outbound": True}, model_output="ignored"
    )

    assert rendered == []


@pytest.mark.asyncio
async def test_framework_collect_outbounds_returns_empty_when_fallback_is_suppressed() -> None:
    framework = BubFramework()

    async def call_many(name: str, **kwargs: object) -> list[object]:
        assert name == "render_outbound"
        return []

    framework._hook_runtime.call_many = call_many  # type: ignore[method-assign]

    outbounds = await framework._collect_outbounds(
        message={"channel": "lark", "chat_id": "oc_123"},
        session_id="lark:acct:oc_123:main",
        state={"_suppress_default_outbound": True},
        model_output='{"ok": true}',
    )

    assert outbounds == []
