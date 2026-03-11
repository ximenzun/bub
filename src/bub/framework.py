"""Hook-first Bub framework runtime."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pluggy
import typer
from loguru import logger
from republic import AsyncTapeStore
from republic.tape import TapeStore

from bub.envelope import content_of, field_of, unpack_batch
from bub.hook_runtime import HookRuntime
from bub.hookspecs import BUB_HOOK_NAMESPACE, BubHookSpecs
from bub.social import ConversationRef, OutboundAction, ReplyGrant, normalize_surface
from bub.social.types import ActionKind, ContentKind
from bub.types import Envelope, MessageHandler, ModelEvent, ModelStream, OutboundChannelRouter, TurnResult

if TYPE_CHECKING:
    from bub.channels.base import Channel
    from bub.model_backend import ModelBackend


@dataclass(frozen=True)
class PluginStatus:
    is_success: bool
    detail: str | None = None


class BubFramework:
    """Minimal framework core. Everything grows from hook skills."""

    def __init__(self) -> None:
        self.workspace = Path.cwd().resolve()
        self._plugin_manager = pluggy.PluginManager(BUB_HOOK_NAMESPACE)
        self._plugin_manager.add_hookspecs(BubHookSpecs)
        self._hook_runtime = HookRuntime(self._plugin_manager)
        self._plugin_status: dict[str, PluginStatus] = {}
        self._outbound_router: OutboundChannelRouter | None = None

    def _load_builtin_hooks(self) -> None:
        from bub.builtin.hook_impl import BuiltinImpl

        impl = BuiltinImpl(self)

        try:
            self._plugin_manager.register(impl, name="builtin")
        except Exception as exc:
            self._plugin_status["builtin"] = PluginStatus(is_success=False, detail=str(exc))
        else:
            self._plugin_status["builtin"] = PluginStatus(is_success=True)

    def load_hooks(self) -> None:
        import importlib.metadata

        self._load_builtin_hooks()
        for entry_point in importlib.metadata.entry_points(group="bub"):
            try:
                plugin = entry_point.load()
                if callable(plugin):  # Support entry points that are classes
                    plugin = plugin(self)
                self._plugin_manager.register(plugin, name=entry_point.name)
            except Exception as exc:
                logger.warning(f"Failed to load plugin '{entry_point.name}': {exc}")
                self._plugin_status[entry_point.name] = PluginStatus(is_success=False, detail=str(exc))
            else:
                self._plugin_status[entry_point.name] = PluginStatus(is_success=True)

    def create_cli_app(self) -> typer.Typer:
        """Create CLI app by collecting commands from hooks. Can be used for custom CLI entry point."""
        app = typer.Typer(name="bub", help="Batteries-included, hook-first AI framework", add_completion=False)

        @app.callback(invoke_without_command=True)
        def _main(
            ctx: typer.Context,
            workspace: str | None = typer.Option(None, "--workspace", "-w", help="Path to the workspace"),
        ) -> None:
            if workspace:
                self.workspace = Path(workspace).resolve()
            ctx.obj = self

        self._hook_runtime.call_many_sync("register_cli_commands", app=app)
        return app

    async def process_inbound(self, inbound: Envelope) -> TurnResult:
        """Run one inbound message through hooks and return turn result."""

        try:
            session_id = await self._hook_runtime.call_first(
                "resolve_session", message=inbound
            ) or self._default_session_id(inbound)
            if isinstance(inbound, dict):
                inbound.setdefault("session_id", session_id)
            state = {"_runtime_workspace": str(self.workspace)}
            for hook_state in reversed(
                await self._hook_runtime.call_many("load_state", message=inbound, session_id=session_id)
            ):
                if isinstance(hook_state, dict):
                    state.update(hook_state)
            prompt = await self._hook_runtime.call_first(
                "build_prompt", message=inbound, session_id=session_id, state=state
            )
            if not prompt:
                prompt = content_of(inbound)
            model_output = ""
            streamed_actions: list[OutboundAction] = []
            try:
                model_stream = await self._hook_runtime.call_first(
                    "run_model_stream", prompt=prompt, session_id=session_id, state=state
                )
                if model_stream is None:
                    await self._hook_runtime.notify_error(
                        stage="run_model_stream:fallback",
                        error=RuntimeError("no model stream returned output"),
                        message=inbound,
                    )
                    model_output = prompt if isinstance(prompt, str) else content_of(inbound)
                else:
                    model_output, streamed_actions = await self._consume_model_stream(model_stream)
            finally:
                await self._hook_runtime.call_many(
                    "save_state",
                    session_id=session_id,
                    state=state,
                    message=inbound,
                    model_output=model_output,
                )

            outbound_actions = await self._collect_outbound_actions(inbound, session_id, state, model_output)
            for action in outbound_actions:
                await self._dispatch_action(action)
            return TurnResult(
                session_id=session_id,
                prompt=prompt,
                model_output=model_output,
                outbound_actions=[*streamed_actions, *outbound_actions],
            )
        except Exception as exc:
            await self._hook_runtime.notify_error(stage="turn", error=exc, message=inbound)
            raise

    def hook_report(self) -> dict[str, list[str]]:
        """Return hook implementation summary for diagnostics."""

        return self._hook_runtime.hook_report()

    def bind_outbound_router(self, router: OutboundChannelRouter | None) -> None:
        self._outbound_router = router

    async def dispatch_via_router(self, action: OutboundAction) -> bool:
        if self._outbound_router is None:
            return False
        return await self._outbound_router.dispatch(action)

    @staticmethod
    def _default_session_id(message: Envelope) -> str:
        session_id = field_of(message, "session_id")
        if session_id is not None:
            return str(session_id)
        channel = str(field_of(message, "channel", "default"))
        chat_id = str(field_of(message, "chat_id", "default"))
        return f"{channel}:{chat_id}"

    async def _collect_outbound_actions(
        self,
        message: Envelope,
        session_id: str,
        state: dict[str, Any],
        model_output: str,
    ) -> list[OutboundAction]:
        batches = await self._hook_runtime.call_many(
            "render_actions",
            message=message,
            session_id=session_id,
            state=state,
            model_output=model_output,
        )
        outbound_actions: list[OutboundAction] = []
        for batch in batches:
            for item in unpack_batch(batch):
                if item is None:
                    continue
                if isinstance(item, OutboundAction):
                    outbound_actions.append(item)
                    continue
                if isinstance(item, Mapping):
                    outbound_actions.append(OutboundAction.from_mapping(item))
                    continue
                raise TypeError(f"Unsupported outbound action type: {type(item)!r}")
        if outbound_actions:
            return outbound_actions

        fallback_action = self._default_outbound_action(message, model_output)
        if fallback_action is None:
            return []
        return [fallback_action]

    async def _consume_model_stream(self, model_stream: ModelStream | Iterable[ModelEvent]) -> tuple[str, list[OutboundAction]]:
        text_parts: list[str] = []
        streamed_actions: list[OutboundAction] = []
        if hasattr(model_stream, "__aiter__"):
            async for event in cast(ModelStream, model_stream):
                await self._handle_model_event(event, text_parts, streamed_actions)
        else:
            for event in model_stream:
                await self._handle_model_event(event, text_parts, streamed_actions)
        return "".join(text_parts), streamed_actions

    async def _handle_model_event(
        self,
        event: ModelEvent,
        text_parts: list[str],
        streamed_actions: list[OutboundAction],
    ) -> None:
        if not isinstance(event, ModelEvent):
            raise TypeError(f"Unsupported model event type: {type(event)!r}")
        if event.kind == "text_delta":
            text_parts.append(event.text)
            return
        if event.kind == "action":
            if event.action is None:
                raise ValueError("ModelEvent(kind='action') requires action")
            streamed_actions.append(event.action)
            await self._dispatch_action(event.action)
            return
        raise ValueError(f"Unsupported model event kind: {event.kind!r}")

    async def _dispatch_action(self, action: OutboundAction) -> bool:
        hook_results = await self._hook_runtime.call_many("dispatch_outbound", action=action)
        if any(result is True for result in hook_results):
            return True
        return await self.dispatch_via_router(action)

    @staticmethod
    def _default_outbound_action(message: Envelope, model_output: str) -> OutboundAction | None:
        if not model_output.strip():
            return None
        raw_conversation = field_of(message, "conversation")
        if isinstance(raw_conversation, ConversationRef):
            conversation = raw_conversation
        elif isinstance(raw_conversation, Mapping):
            conversation = ConversationRef.from_mapping(raw_conversation)
        else:
            platform = str(field_of(message, "output_channel", field_of(message, "channel", "default")))
            conversation = ConversationRef(
                platform=platform,
                chat_id=str(field_of(message, "chat_id", "default")),
                account_id=str(field_of(message, "account_id", "default")),
                surface=normalize_surface(field_of(message, "surface", field_of(message, "chat_type", "unknown"))),
                thread_id=_string_or_none(field_of(message, "thread_id")),
                lane_id=_string_or_none(field_of(message, "lane_id")),
                actor_id=_string_or_none(field_of(message, "actor_id")),
                tenant_id=_string_or_none(field_of(message, "tenant_id")),
                metadata=dict(field_of(message, "conversation_metadata", {}) or {}),
            )

        raw_reply_grant = field_of(message, "reply_grant")
        if isinstance(raw_reply_grant, ReplyGrant):
            reply_grant = raw_reply_grant
        elif isinstance(raw_reply_grant, Mapping):
            reply_grant = ReplyGrant.from_mapping(raw_reply_grant)
        else:
            reply_grant = None

        reply_to_message_id = _string_or_none(field_of(message, "reply_to_message_id"))
        if reply_to_message_id is None and reply_grant is not None:
            reply_to_message_id = reply_grant.reply_to_message_id
        kind: ActionKind = "reply_message" if reply_to_message_id else "send_message"
        content_type: ContentKind = cast(ContentKind, str(field_of(message, "content_type", "text")))
        return OutboundAction(
            kind=kind,
            conversation=conversation,
            text=model_output,
            content_type=content_type,
            message_id=_string_or_none(field_of(message, "message_id")),
            reply_to_message_id=reply_to_message_id,
            reply_grant=reply_grant,
            metadata={
                "message_kind": str(field_of(message, "kind", "normal")),
                **dict(field_of(message, "metadata", {}) or {}),
            },
        )

    def get_channels(self, message_handler: MessageHandler) -> dict[str, Channel]:
        channels: dict[str, Channel] = {}
        for result in self._hook_runtime.call_many_sync("provide_channels", message_handler=message_handler):
            for channel in result:
                if channel.name not in channels:
                    channels[channel.name] = channel
        return channels

    def get_tape_store(self) -> TapeStore | AsyncTapeStore | None:
        return self._hook_runtime.call_first_sync("provide_tape_store")

    def get_model_backend(self) -> ModelBackend | None:
        return self._hook_runtime.call_first_sync("provide_model_backend")

    def get_system_prompt(self, prompt: str, state: dict[str, Any]) -> str:
        return "\n\n".join(
            result
            for result in reversed(self._hook_runtime.call_many_sync("system_prompt", prompt=prompt, state=state))
            if result
        )


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
