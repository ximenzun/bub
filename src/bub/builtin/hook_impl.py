import base64
import sys
from pathlib import Path
from typing import cast

import typer
from loguru import logger
from republic.tape import TapeStore

from bub.builtin.agent import Agent
from bub.channels.base import Channel
from bub.channels.message import ChannelMessage, MediaItem, MessageKind
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
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

**IMPORTANT:** On Bub's native inbound channels (`cli`, `telegram`, and compatible native plugins such as `lark` or `wecom_longconn_bot`), your final plain text answer is routed automatically back to the same conversation.
Do NOT call channel scripts or channel skills for an ordinary reply on those inbound native sessions.
Use channel-specific skills or native-action tools only when you need a proactive message, a card/template update, an edit, or another channel-only capability that plain text cannot express.

When responding to a channel message, you MUST:
1. Identify the channel from the message metadata (e.g., `$telegram`, `$lark`, `$wecom_longconn_bot`)
2. Use the native Bub reply path for ordinary replies on native inbound channels
3. Use the channel skill or native-action tool only for proactive sends or special channel-native actions
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
        inbound_context = dict(field_of(message, "context", {}) or {})
        state = {
            "session_id": session_id,
            "_runtime_agent": self.agent,
            "_inbound_message": message,
            "_inbound_channel": field_of(message, "channel", "default"),
            "_inbound_output_channel": field_of(message, "output_channel", field_of(message, "channel", "default")),
            "_inbound_chat_id": field_of(message, "chat_id", "default"),
            "_inbound_kind": field_of(message, "kind", "normal"),
            "_inbound_context": inbound_context,
        }
        for state_key, context_key in (
            ("_inbound_account_id", "account_id"),
            ("_inbound_actor_id", "actor_id"),
            ("_inbound_message_id", "message_id"),
            ("_inbound_thread_id", "thread_id"),
            ("_inbound_tenant_id", "tenant_id"),
            ("_inbound_surface", "surface"),
        ):
            value = inbound_context.get(context_key)
            if value is not None:
                state[state_key] = value
        if field_of(message, "message_id") is not None:
            state["_inbound_message_id"] = field_of(message, "message_id")
        if field_of(message, "account_id", "default") != "default":
            state["_inbound_account_id"] = field_of(message, "account_id")
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
    async def build_prompt(self, message: ChannelMessage, session_id: str, state: State) -> str | list[dict]:
        content = content_of(message)
        if content.startswith(","):
            message.kind = "command"
            return content
        context = field_of(message, "context_str")
        context_prefix = f"{context}\n---\n" if context else ""
        text = f"{context_prefix}{content}"

        media = field_of(message, "media") or []
        if not media:
            return text

        media_parts: list[dict] = []
        for item in cast("list[MediaItem]", media):
            match item.type:
                case "image":
                    if item.data_fetcher is None:
                        continue
                    data = await item.data_fetcher()
                    data_url = f"data:{item.mime_type};base64,{base64.b64encode(data).decode('utf-8')}"
                    media_parts.append({"type": "image_url", "image_url": {"url": data_url}})
                case _:
                    pass  # TODO: Not supported for now
        if media_parts:
            return [{"type": "text", "text": text}, *media_parts]
        return text

    @hookimpl
    async def run_model(self, prompt: str | list[dict], session_id: str, state: State) -> str:
        return await self.agent.run(session_id=session_id, prompt=prompt, state=state)

    @hookimpl
    def register_cli_commands(self, app: typer.Typer) -> None:
        from bub.builtin import cli

        app.command("run")(cli.run)
        app.command("chat")(cli.chat)
        app.command("login")(cli.login)
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
    def system_prompt(self, prompt: str | list[dict], state: State) -> str:
        # Read the content of AGENTS.md under workspace
        return DEFAULT_SYSTEM_PROMPT + "\n\n" + self._read_agents_file(state)

    @hookimpl
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        from bub.channels.cli import CliChannel
        from bub.channels.telegram import TelegramChannel

        slash_commands = [(command.name, command.summary) for command in self.framework.get_slash_commands()]
        try:
            telegram = TelegramChannel(on_receive=message_handler, slash_commands=slash_commands)
        except TypeError:
            telegram = TelegramChannel(on_receive=message_handler)
        return [
            telegram,
            CliChannel(on_receive=message_handler, agent=self.agent),
        ]

    @hookimpl
    async def on_error(self, stage: str, error: Exception, message: Envelope | None) -> None:
        if message is not None:
            outbound = self._build_outbound_message(
                message=message,
                session_id=field_of(message, "session_id", "unknown"),
                model_output=f"An error occurred at stage '{stage}': {error}",
                kind="error",
            )
            await self.framework._hook_runtime.call_many("dispatch_outbound", message=outbound)

    @hookimpl
    async def dispatch_outbound(self, message: Envelope) -> bool:
        content = content_of(message)
        session_id = field_of(message, "session_id")
        if field_of(message, "output_channel") != "cli":
            logger.info("session.run.outbound session_id={} content={}", session_id, content)
        return await self.framework.dispatch_via_router(message)

    @hookimpl
    def render_outbound(
        self,
        message: Envelope,
        session_id: str,
        state: State,
        model_output: str,
    ) -> list[ChannelMessage]:
        del state
        return [
            self._build_outbound_message(
                message=message,
                session_id=session_id,
                model_output=model_output,
                kind=cast(MessageKind, field_of(message, "kind", "normal")),
            )
        ]

    @hookimpl
    def provide_tape_store(self) -> TapeStore:
        from bub.builtin.store import FileTapeStore

        return FileTapeStore(directory=self.agent.settings.home / "tapes")

    def _build_outbound_message(
        self,
        *,
        message: Envelope,
        session_id: str,
        model_output: str,
        kind: MessageKind,
    ) -> ChannelMessage:
        output_channel = str(field_of(message, "output_channel", field_of(message, "channel", "default")))
        context = _default_outbound_context(message)
        account_id = _context_string(context, "account_id") or str(field_of(message, "account_id", "default"))
        outbound = ChannelMessage(
            session_id=session_id,
            channel=output_channel,
            chat_id=str(field_of(message, "chat_id", "default")),
            content=model_output,
            output_channel=output_channel,
            kind=kind,
            account_id=account_id,
            context=context,
        )
        message_id = _context_string(context, "message_id")
        if message_id is not None:
            outbound.message_id = message_id
        return outbound


def _default_outbound_context(message: Envelope) -> dict[str, object]:
    inbound_context = field_of(message, "context", {})
    context = dict(inbound_context) if isinstance(inbound_context, dict) else {}

    outbound: dict[str, object] = {}
    for key in (
        "account_id",
        "actor_id",
        "attachment",
        "card",
        "content_type",
        "message_thread_id",
        "surface",
        "telegram_kind",
        "tenant_id",
        "thread_id",
        "wecom_event_type",
        "wecom_raw_msgtype",
        "wecom_reply_token",
        "wecom_response_url",
        "wecom_sender_id_kind",
        "wecom_message_id",
    ):
        if key in context and context[key] is not None:
            outbound[key] = context[key]

    if "reply_to_message_id" in context and context["reply_to_message_id"] is not None:
        outbound["reply_to_message_id"] = context["reply_to_message_id"]
    else:
        reply_to = field_of(message, "message_id")
        if reply_to is None:
            reply_to = context.get("message_id")
        if reply_to is not None:
            outbound["reply_to_message_id"] = reply_to

    return outbound


def _context_string(context: dict[str, object], key: str) -> str | None:
    value = context.get(key)
    if value is None:
        return None
    text = str(value)
    return text if text else None
