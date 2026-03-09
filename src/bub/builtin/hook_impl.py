import sys
from pathlib import Path

import typer
from loguru import logger
from republic.tape import TapeStore

from bub.builtin.agent import Agent
from bub.channels.base import Channel
from bub.channels.message import ChannelMessage
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.social import ConversationRef, OutboundAction, ReplyGrant, normalize_surface
from bub.types import Envelope, MessageHandler, State

AGENTS_FILE_NAME = "AGENTS.md"
DEFAULT_SYSTEM_PROMPT = """\
<general_instruct>
Call tools or skills to finish the task.
</general_instruct>
<response_instruct>
Before ending the run, you MUST determine whether a response needs to be sent to the channel, checking the following conditions:
1. Has the user asked you a question waiting for your answer?
2. Is there any error or important information that needs to be sent to the user immediately?
3. If it is a casual chat, does the conversation need to be continued?

**IMPORTANT:** Your plain/direct reply in this chat will be ignored.
**Therefore, you MUST send messages via channel using the correct skill if a response is needed.**

When responding to a channel message, you MUST:
1. Identify the channel from the message metadata (e.g., `$telegram`, `$discord`)
2. Send the message as instructed by the channel skill (e.g., `telegram` skill for `$telegram` channel)
</response_instruct>
<context_contract>
Excessively long context may cause model call failures. In this case, you MAY use tape.info to the token usage and you SHOULD use tape.handoff tool to shorten the length of the retrieved history.
</context_contract>
"""


class BuiltinImpl:
    """Default hook implementations for basic runtime operations."""

    def __init__(self, framework: BubFramework) -> None:
        from bub.builtin import tools  # noqa: F401

        self.framework = framework
        self.agent = Agent(framework)

    @hookimpl
    def resolve_session(self, message: ChannelMessage) -> str:
        session_id = field_of(message, "session_id")
        if session_id is not None and str(session_id).strip():
            return str(session_id)
        channel = str(field_of(message, "channel", "default"))
        chat_id = str(field_of(message, "chat_id", "default"))
        return f"{channel}:{chat_id}"

    @hookimpl
    async def load_state(self, message: ChannelMessage, session_id: str) -> State:
        lifespan = field_of(message, "lifespan")
        if lifespan is not None:
            await lifespan.__aenter__()
        state = {"session_id": session_id, "_runtime_agent": self.agent}
        if context := field_of(message, "context_str"):
            state["context"] = context
        return state

    @hookimpl
    async def save_state(self, session_id: str, state: State, message: ChannelMessage, model_output: str) -> None:
        tp, value, traceback = sys.exc_info()
        lifespan = field_of(message, "lifespan")
        if lifespan is not None:
            await lifespan.__aexit__(tp, value, traceback)

    @hookimpl
    def build_prompt(self, message: ChannelMessage, session_id: str, state: State) -> str:
        content = content_of(message)
        if content.startswith(","):
            message.kind = "command"
            return content
        context = field_of(message, "context_str")
        context_prefix = f"{context}\n---\n" if context else ""
        return f"{context_prefix}{content}"

    @hookimpl
    async def run_model(self, prompt: str, session_id: str, state: State) -> str:
        return await self.agent.run(session_id=session_id, prompt=prompt, state=state)

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        from bub.builtin import cli

        app.command("run")(cli.run)
        app.command("chat")(cli.chat)
        app.command("hooks", hidden=True)(cli.list_hooks)
        app.command("message", hidden=True)(app.command("gateway")(cli.gateway))

    def _read_agents_file(self, state: State) -> str:
        workspace = state.get("_runtime_workspace", str(Path.cwd()))
        prompt_path = Path(workspace) / AGENTS_FILE_NAME
        if not prompt_path.is_file():
            return ""
        try:
            return prompt_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @hookimpl
    def system_prompt(self, prompt: str, state: State) -> str:
        # Read the content of AGENTS.md under workspace
        return DEFAULT_SYSTEM_PROMPT + "\n\n" + self._read_agents_file(state)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        from bub.channels.cli import CliChannel
        from bub.channels.telegram import TelegramChannel

        return [
            TelegramChannel(on_receive=message_handler),
            CliChannel(on_receive=message_handler, agent=self.agent),
        ]

    @hookimpl
    async def on_error(self, stage: str, error: Exception, message: Envelope | None) -> None:
        if message is not None:
            action = OutboundAction(
                kind="reply_message" if self._reply_to_message_id(message) else "send_message",
                conversation=self._conversation_for(message),
                text=f"An error occurred at stage '{stage}': {error}",
                reply_to_message_id=self._reply_to_message_id(message),
                reply_grant=self._reply_grant_for(message),
                metadata=field_of(message, "metadata", {}),
            )
            action.metadata["message_kind"] = "error"
            await self.framework._hook_runtime.call_many("dispatch_outbound", action=action)

    @hookimpl
    async def dispatch_outbound(self, action: OutboundAction) -> bool:
        target = action.conversation.platform if action.conversation is not None else "unknown"
        chat_id = action.conversation.chat_id if action.conversation is not None else "unknown"
        preview = (action.text or "").strip()
        if preview and len(preview) > 100:
            preview = preview[:97] + "..."
        if target != "cli":
            logger.info(
                "session.run.outbound action_kind={} target={} chat_id={} text={}",
                action.kind,
                target,
                chat_id,
                preview,
            )
        return await self.framework.dispatch_via_router(action)

    @hookimpl
    def render_actions(
        self,
        message: Envelope,
        session_id: str,
        state: State,
        model_output: str,
    ) -> list[OutboundAction]:
        action = OutboundAction(
            kind="reply_message" if self._reply_to_message_id(message) else "send_message",
            conversation=self._conversation_for(message),
            text=model_output,
            content_type=str(field_of(message, "content_type", "text")),
            message_id=_string_or_none(field_of(message, "message_id")),
            reply_to_message_id=self._reply_to_message_id(message),
            reply_grant=self._reply_grant_for(message),
            metadata={
                "message_kind": str(field_of(message, "kind", "normal")),
                **dict(field_of(message, "metadata", {}) or {}),
            },
        )
        return [action]

    @hookimpl
    def provide_tape_store(self) -> TapeStore:
        from bub.builtin.store import FileTapeStore

        return FileTapeStore(directory=self.agent.settings.home / "tapes")

    @staticmethod
    def _conversation_for(message: Envelope) -> ConversationRef:
        raw = field_of(message, "conversation")
        if isinstance(raw, ConversationRef):
            conversation = raw
        elif isinstance(raw, dict):
            conversation = ConversationRef.from_mapping(raw)
        else:
            conversation = ConversationRef(
                platform=str(field_of(message, "output_channel", field_of(message, "channel", "default"))),
                chat_id=str(field_of(message, "chat_id", "default")),
                account_id=str(field_of(message, "account_id", "default")),
                surface=normalize_surface(field_of(message, "surface", field_of(message, "chat_type", "unknown"))),
                thread_id=_string_or_none(field_of(message, "thread_id")),
                lane_id=_string_or_none(field_of(message, "lane_id")),
                actor_id=_string_or_none(field_of(message, "actor_id")),
                tenant_id=_string_or_none(field_of(message, "tenant_id")),
                metadata=dict(field_of(message, "conversation_metadata", {}) or {}),
            )
        output_channel = field_of(message, "output_channel")
        if output_channel:
            conversation.platform = str(output_channel)
        return conversation

    @staticmethod
    def _reply_grant_for(message: Envelope) -> ReplyGrant | None:
        raw = field_of(message, "reply_grant")
        if isinstance(raw, ReplyGrant):
            return raw
        if isinstance(raw, dict):
            return ReplyGrant.from_mapping(raw)
        return None

    @classmethod
    def _reply_to_message_id(cls, message: Envelope) -> str | None:
        reply_to = field_of(message, "reply_to_message_id")
        if reply_to is not None:
            return str(reply_to)
        reply_grant = cls._reply_grant_for(message)
        if reply_grant is None:
            return None
        return reply_grant.reply_to_message_id


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    return str(value)
