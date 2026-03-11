from __future__ import annotations

from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from bub.channels.base import Channel
from bub.commands import SlashCommandSpec
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.social import ConversationRef, OutboundAction
from bub.types import ModelEvent


class NamedChannel(Channel):
    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label

    async def start(self, stop_event) -> None:
        return None

    async def stop(self) -> None:
        return None


def test_create_cli_app_sets_workspace_and_context(tmp_path: Path) -> None:
    framework = BubFramework()

    class CliPlugin:
        @hookimpl
        def register_cli_commands(self, app: typer.Typer) -> None:
            @app.command("workspace")
            def workspace_command(ctx: typer.Context) -> None:
                current = ctx.ensure_object(BubFramework)
                typer.echo(str(current.workspace))

    framework._plugin_manager.register(CliPlugin(), name="cli-plugin")
    app = framework.create_cli_app()

    result = CliRunner().invoke(app, ["--workspace", str(tmp_path), "workspace"])

    assert result.exit_code == 0
    assert result.stdout.strip() == str(tmp_path.resolve())
    assert framework.workspace == tmp_path.resolve()


def test_get_channels_prefers_high_priority_plugin_for_duplicate_names() -> None:
    framework = BubFramework()

    class LowPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [NamedChannel("shared", "low"), NamedChannel("low-only", "low")]

    class HighPriorityPlugin:
        @hookimpl
        def provide_channels(self, message_handler):
            return [NamedChannel("shared", "high"), NamedChannel("high-only", "high")]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    channels = framework.get_channels(lambda message: None)

    assert set(channels) == {"shared", "low-only", "high-only"}
    assert channels["shared"].label == "high"
    assert channels["low-only"].label == "low"
    assert channels["high-only"].label == "high"


def test_get_system_prompt_uses_priority_order_and_skips_empty_results() -> None:
    framework = BubFramework()

    class LowPriorityPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str:
            return "low"

    class HighPriorityPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str | None:
            return "high"

    class EmptyPlugin:
        @hookimpl
        def system_prompt(self, prompt: str, state: dict[str, str]) -> str | None:
            return None

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")
    framework._plugin_manager.register(EmptyPlugin(), name="empty")

    prompt = framework.get_system_prompt(prompt="hello", state={})

    assert prompt == "low\n\nhigh"


def test_get_model_backend_prefers_high_priority_plugin() -> None:
    framework = BubFramework()
    low_backend = object()
    high_backend = object()

    class LowPriorityPlugin:
        @hookimpl
        def provide_model_backend(self):
            return low_backend

    class HighPriorityPlugin:
        @hookimpl
        def provide_model_backend(self):
            return high_backend

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    assert framework.get_model_backend() is high_backend


def test_get_slash_commands_prefers_high_priority_plugin_for_duplicate_names() -> None:
    framework = BubFramework()
    low = SlashCommandSpec(name="/repo", summary="low")
    high = SlashCommandSpec(name="/repo", summary="high")
    git = SlashCommandSpec(name="/git", summary="git")

    class LowPriorityPlugin:
        @hookimpl
        def provide_slash_commands(self):
            return [low]

    class HighPriorityPlugin:
        @hookimpl
        def provide_slash_commands(self):
            return [high, git]

    framework._plugin_manager.register(LowPriorityPlugin(), name="low")
    framework._plugin_manager.register(HighPriorityPlugin(), name="high")

    commands = framework.get_slash_commands()

    assert commands == [git, high]


def test_builtin_cli_exposes_gateway_and_keeps_message_hidden_alias() -> None:
    framework = BubFramework()
    framework.load_hooks()
    app = framework.create_cli_app()
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    alias_result = runner.invoke(app, ["message", "--help"])

    assert help_result.exit_code == 0
    assert "gateway" in help_result.stdout
    assert "│ message" not in help_result.stdout
    assert alias_result.exit_code == 0
    assert "bub message" in alias_result.stdout
    assert "Start message listeners" in alias_result.stdout


@pytest.mark.asyncio
async def test_process_inbound_falls_back_to_native_outbound_action() -> None:
    framework = BubFramework()

    class Plugin:
        @hookimpl
        def resolve_session(self, message):
            return "cli:room"

        @hookimpl
        def load_state(self, message, session_id):
            return {}

        @hookimpl
        def build_prompt(self, message, session_id, state):
            return "prompt"

        @hookimpl
        async def run_model_stream(self, prompt, session_id, state):
            yield ModelEvent(kind="text_delta", text="hello")

    framework._plugin_manager.register(Plugin(), name="plugin")

    result = await framework.process_inbound({"channel": "cli", "chat_id": "room", "content": "ignored"})

    assert result.outbound_actions == [
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="cli", chat_id="room", account_id="default"),
            text="hello",
            content_type="text",
            metadata={"message_kind": "normal"},
        )
    ]


@pytest.mark.asyncio
async def test_process_inbound_dispatches_streamed_actions_before_final_actions() -> None:
    framework = BubFramework()
    dispatched: list[OutboundAction] = []

    class Router:
        async def dispatch(self, action: OutboundAction) -> bool:
            dispatched.append(action)
            return True

    class Plugin:
        @hookimpl
        def resolve_session(self, message):
            return "telegram:42"

        @hookimpl
        def load_state(self, message, session_id):
            return {}

        @hookimpl
        def build_prompt(self, message, session_id, state):
            return "prompt"

        @hookimpl
        async def run_model_stream(self, prompt, session_id, state):
            yield ModelEvent(
                kind="action",
                action=OutboundAction(
                    kind="presence",
                    conversation=ConversationRef(platform="telegram", chat_id="42", account_id="default"),
                ),
            )
            yield ModelEvent(kind="text_delta", text="hello")

    framework._plugin_manager.register(Plugin(), name="plugin")
    framework.bind_outbound_router(Router())

    result = await framework.process_inbound({"channel": "telegram", "chat_id": "42", "content": "ignored"})

    assert dispatched == [
        OutboundAction(
            kind="presence",
            conversation=ConversationRef(platform="telegram", chat_id="42", account_id="default"),
        ),
        OutboundAction(
            kind="send_message",
            conversation=ConversationRef(platform="telegram", chat_id="42", account_id="default"),
            text="hello",
            content_type="text",
            metadata={"message_kind": "normal"},
        ),
    ]
    assert result.outbound_actions == dispatched


@pytest.mark.asyncio
async def test_process_inbound_does_not_emit_blank_final_action_when_stream_only_emits_actions() -> None:
    framework = BubFramework()
    dispatched: list[OutboundAction] = []

    class Router:
        async def dispatch(self, action: OutboundAction) -> bool:
            dispatched.append(action)
            return True

    class Plugin:
        @hookimpl
        def resolve_session(self, message):
            return "telegram:42"

        @hookimpl
        def load_state(self, message, session_id):
            return {}

        @hookimpl
        def build_prompt(self, message, session_id, state):
            return "prompt"

        @hookimpl
        async def run_model_stream(self, prompt, session_id, state):
            yield ModelEvent(
                kind="action",
                action=OutboundAction(
                    kind="presence",
                    conversation=ConversationRef(platform="telegram", chat_id="42", account_id="default"),
                ),
            )

    framework._plugin_manager.register(Plugin(), name="plugin")
    framework.bind_outbound_router(Router())

    result = await framework.process_inbound({"channel": "telegram", "chat_id": "42", "content": "ignored"})

    assert dispatched == [
        OutboundAction(
            kind="presence",
            conversation=ConversationRef(platform="telegram", chat_id="42", account_id="default"),
        )
    ]
    assert result.outbound_actions == dispatched
