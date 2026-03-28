import asyncio
import contextlib
from collections.abc import Collection
from dataclasses import replace
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from bub.channels.base import Channel
from bub.channels.handler import BufferedMessageHandler
from bub.channels.message import ChannelMessage
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.social import OutboundAction
from bub.types import Envelope, MessageHandler
from bub.utils import wait_until_stopped


class ChannelSettings(BaseModel):
    enabled_channels: str | list[str] = Field(
        default="all",
        description="Comma-separated list of enabled channels, or 'all' for all channels.",
    )
    debounce_seconds: float = Field(
        default=1.0,
        description="Minimum seconds between processing two messages from the same channel to prevent overload.",
    )
    max_wait_seconds: float = Field(
        default=10.0,
        description="Maximum seconds to wait for processing before new messages reach the channel.",
    )
    active_time_window: float = Field(
        default=60.0,
        description="Time window in seconds to consider a channel active for processing messages.",
    )


class ChannelManager:
    def __init__(self, framework: BubFramework, enabled_channels: Collection[str] | None = None) -> None:
        self.framework = framework
        self._channels: dict[str, Channel] = {}
        self._enabled_channels_override = list(enabled_channels) if enabled_channels is not None else None
        self._settings = ChannelSettings()
        self._enabled_channels: list[str] = []
        self._channel_revisions: dict[str, str] = {}
        self._messages = asyncio.Queue[ChannelMessage]()
        self._ongoing_tasks: set[asyncio.Task] = set()
        self._session_handlers: dict[str, MessageHandler] = {}
        self._started_channels: set[str] = set()
        self._reload_runtime_configuration()
        self._channels = self.framework.get_channels(self.on_receive)

    async def on_receive(self, message: ChannelMessage) -> None:
        channel = message.channel
        session_id = message.session_id
        if channel not in self._channels:
            logger.warning(f"Received message from unknown channel '{channel}', ignoring.")
            return
        if session_id not in self._session_handlers:
            handler: MessageHandler
            if self._channels[channel].needs_debounce:
                handler = BufferedMessageHandler(
                    self._messages.put,
                    active_time_window=self._settings.active_time_window,
                    max_wait_seconds=self._settings.max_wait_seconds,
                    debounce_seconds=self._settings.debounce_seconds,
                )
            else:
                handler = self._messages.put
            self._session_handlers[session_id] = handler
        await self._session_handlers[session_id](message)

    def get_channel(self, name: str) -> Channel | None:
        return self._channels.get(name)

    async def dispatch(self, message: Envelope) -> bool:
        if isinstance(message, OutboundAction):
            outbound = _channel_message_from_action(message)
            if outbound is None:
                return False
            channel = self.get_channel(outbound.channel)
            if channel is None:
                return False
            await channel.send(outbound)
            return True

        if isinstance(message, ChannelMessage):
            channel_name = message.output_channel or message.channel
            channel = self.get_channel(channel_name)
            if channel is None:
                return False
            outbound = (
                message
                if message.channel == channel_name and message.output_channel == channel_name
                else replace(message, channel=channel_name, output_channel=channel_name)
            )
            await channel.send(outbound)
            return True

        channel_name = field_of(message, "output_channel", field_of(message, "channel"))
        if channel_name is None:
            return False

        channel_key = str(channel_name)
        channel = self.get_channel(channel_key)
        if channel is None:
            return False

        outbound = ChannelMessage(
            session_id=str(field_of(message, "session_id", f"{channel_key}:default")),
            channel=channel_key,
            chat_id=str(field_of(message, "chat_id", "default")),
            content=content_of(message),
            context=field_of(message, "context", {}),
            kind=field_of(message, "kind", "normal"),
            media=field_of(message, "media", []),
            output_channel=str(field_of(message, "output_channel", channel_key)),
            account_id=str(field_of(message, "account_id", "default")),
            message_id=_string_or_none(field_of(message, "message_id")),
            conversation=field_of(message, "conversation"),
            sender=field_of(message, "sender"),
            reply_grant=field_of(message, "reply_grant"),
            attachments=field_of(message, "attachments", []),
            actions=field_of(message, "actions", []),
            metadata=field_of(message, "metadata", {}),
        )
        await channel.send(outbound)
        return True

    def enabled_channels(self) -> list[Channel]:
        if "all" in self._enabled_channels:
            return [channel for name, channel in self._channels.items() if name != "cli"]
        return [channel for name, channel in self._channels.items() if name in self._enabled_channels]

    def _enabled_channel_names_for(self, channels: dict[str, Channel]) -> set[str]:
        if "all" in self._enabled_channels:
            return {name for name in channels if name != "cli"}
        return {name for name in channels if name in self._enabled_channels}

    def _reload_runtime_configuration(self) -> None:
        marketplace_getter = getattr(self.framework, "get_marketplace_service", None)
        marketplace = marketplace_getter() if callable(marketplace_getter) else None
        runtime = marketplace.load_runtime("channel_manager") if marketplace is not None else None
        self._settings = ChannelSettings.model_validate(runtime or {})
        self._channel_revisions = self._channel_revisions_for(marketplace)
        if self._enabled_channels_override is not None:
            self._enabled_channels = list(self._enabled_channels_override)
            return
        marketplace_channels = marketplace.enabled_channels() if marketplace is not None else []
        configured_channels = self._settings.enabled_channels
        if isinstance(configured_channels, str):
            fallback_channels = configured_channels.split(",")
        else:
            fallback_channels = list(configured_channels)
        self._enabled_channels = marketplace_channels or fallback_channels

    async def _refresh_runtime(self, stop_event: asyncio.Event) -> None:
        sync_runtime = getattr(self.framework, "sync_runtime", None)
        runtime_changed = bool(sync_runtime()) if callable(sync_runtime) else False
        previous_channels = self._channels
        previous_enabled = list(self._enabled_channels)
        previous_channel_revisions = dict(self._channel_revisions)
        self._reload_runtime_configuration()
        current_channels = self.framework.get_channels(self.on_receive) if runtime_changed else previous_channels
        desired_names = self._enabled_channel_names_for(current_channels)
        active_names = set(self._started_channels)
        restart_names = {
            name
            for name in active_names & desired_names
            if self._channel_needs_restart(
                name=name,
                previous_channels=previous_channels,
                current_channels=current_channels,
                previous_channel_revisions=previous_channel_revisions,
            )
        }
        stop_names = (active_names - desired_names) | restart_names
        start_names = (desired_names - active_names) | restart_names

        for name in sorted((active_names & desired_names) - restart_names):
            previous_channel = previous_channels.get(name)
            if previous_channel is not None:
                current_channels[name] = previous_channel

        for name in sorted(stop_names):
            channel = previous_channels.get(name)
            if channel is None:
                continue
            await channel.stop()
            self._started_channels.discard(name)

        self._channels = current_channels

        for name in sorted(start_names):
            channel = self._channels.get(name)
            if channel is None:
                continue
            await channel.start(stop_event)
            self._started_channels.add(name)

        if runtime_changed or previous_enabled != self._enabled_channels or stop_names or start_names:
            logger.info(
                "channel.manager runtime refreshed runtime_changed={} started={} stopped={}",
                runtime_changed,
                sorted(start_names),
                sorted(stop_names),
            )

    @staticmethod
    def _channel_revisions_for(marketplace: Any) -> dict[str, str]:
        if marketplace is None:
            return {}
        manifests_getter = getattr(marketplace, "manifests", None)
        state_getter = getattr(marketplace, "state", None)
        if not callable(manifests_getter) or not callable(state_getter):
            return {}
        revisions: dict[str, str] = {}
        for manifest in manifests_getter():
            channel_name = getattr(manifest, "channel_name", None)
            plugin_id = getattr(manifest, "plugin_id", None)
            if not isinstance(channel_name, str) or not channel_name:
                continue
            if not isinstance(plugin_id, str) or not plugin_id:
                continue
            state = state_getter(plugin_id)
            if state is None:
                revisions[channel_name] = "missing"
                continue
            updated_at = getattr(state, "updated_at", None)
            enabled = getattr(state, "enabled", False)
            revisions[channel_name] = f"{plugin_id}:{enabled}:{updated_at}"
        return revisions

    def _channel_needs_restart(
        self,
        *,
        name: str,
        previous_channels: dict[str, Channel],
        current_channels: dict[str, Channel],
        previous_channel_revisions: dict[str, str],
    ) -> bool:
        previous_channel = previous_channels.get(name)
        current_channel = current_channels.get(name)
        if previous_channel is None or current_channel is None:
            return False
        if type(previous_channel) is not type(current_channel):
            return True
        return previous_channel_revisions.get(name) != self._channel_revisions.get(name)

    def _on_task_done(self, task: asyncio.Task) -> None:
        task.exception()
        self._ongoing_tasks.discard(task)

    async def _watch_runtime_changes(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(1.0)
            try:
                await self._refresh_runtime(stop_event)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("channel.manager runtime refresh failed")

    async def listen_and_run(self) -> None:
        stop_event = asyncio.Event()
        refresh_task: asyncio.Task[None] | None = None
        self.framework.bind_outbound_router(self)
        await self._refresh_runtime(stop_event)
        refresh_task = asyncio.create_task(self._watch_runtime_changes(stop_event))
        logger.info("channel.manager started listening")
        try:
            while True:
                message = await wait_until_stopped(self._messages.get(), stop_event)
                task = asyncio.create_task(self.framework.process_inbound(message))
                task.add_done_callback(self._on_task_done)
                self._ongoing_tasks.add(task)
        except asyncio.CancelledError:
            logger.info("channel.manager received shutdown signal")
        except Exception:
            logger.exception("channel.manager error")
            raise
        finally:
            stop_event.set()
            if refresh_task is not None:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresh_task
            self.framework.bind_outbound_router(None)
            await self.shutdown()
            logger.info("channel.manager stopped")

    async def shutdown(self) -> None:
        count = 0
        while self._ongoing_tasks:
            task = self._ongoing_tasks.pop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            count += 1
        logger.info(f"channel.manager cancelled {count} in-flight tasks")
        stop_names = self._started_channels | self._enabled_channel_names_for(self._channels)
        for name in sorted(stop_names):
            channel = self._channels.get(name)
            if channel is None:
                continue
            await channel.stop()
        self._started_channels.clear()


def _channel_message_from_action(action: OutboundAction) -> ChannelMessage | None:
    conversation = action.conversation
    if conversation is None:
        return None
    channel_key = conversation.channel_key
    if channel_key == "telegram":
        return _telegram_message_from_action(action)
    if channel_key == "lark":
        return _lark_message_from_action(action)
    if channel_key.startswith("wecom"):
        return _wecom_message_from_action(action)

    return ChannelMessage(
        session_id=f"{channel_key}:{conversation.chat_id}",
        channel=channel_key,
        chat_id=conversation.chat_id,
        content=action.text or "",
        context=_base_context(action),
        output_channel=channel_key,
        account_id=conversation.account_id,
        message_id=action.message_id,
        conversation=conversation,
        reply_grant=action.reply_grant,
        attachments=action.attachments,
        metadata=dict(action.metadata),
    )


def _attachment_source(action: OutboundAction) -> str | None:
    if not action.attachments:
        return None
    attachment = action.attachments[0]
    if attachment.url:
        return attachment.url
    path = attachment.metadata.get("path")
    if path is None:
        return None
    return str(path)


def _base_context(action: OutboundAction) -> dict[str, Any]:
    conversation = action.conversation
    if conversation is None:
        raise ValueError("outbound action requires conversation context")
    context: dict[str, Any] = {}
    reply_to_message_id = action.reply_to_message_id or (
        action.reply_grant.reply_to_message_id if action.reply_grant is not None else None
    )
    if reply_to_message_id is not None:
        context["reply_to_message_id"] = reply_to_message_id
    if action.message_id is not None:
        context["message_id"] = action.message_id
    if conversation.thread_id is not None:
        context["thread_id"] = conversation.thread_id
    if conversation.account_id != "default":
        context["account_id"] = conversation.account_id
    return context


def _telegram_message_from_action(action: OutboundAction) -> ChannelMessage:
    conversation = action.conversation
    if conversation is None:
        raise ValueError("telegram outbound action requires conversation context")
    context = _base_context(action)
    context["telegram_kind"] = action.kind
    if action.live_surface is not None and action.live_surface.surface_id is not None:
        context["surface_id"] = action.live_surface.surface_id
    if action.live_surface is not None and action.live_surface.parent_message_id is not None:
        context.setdefault("message_id", action.live_surface.parent_message_id)
    return ChannelMessage(
        session_id=f"{conversation.channel_key}:{conversation.chat_id}",
        channel=conversation.channel_key,
        chat_id=conversation.chat_id,
        content=action.text or "",
        context=context,
        output_channel=conversation.channel_key,
        account_id=conversation.account_id,
        message_id=action.message_id,
        conversation=conversation,
        reply_grant=action.reply_grant,
        attachments=action.attachments,
        metadata=dict(action.metadata),
    )


def _lark_message_from_action(action: OutboundAction) -> ChannelMessage:
    conversation = action.conversation
    if conversation is None:
        raise ValueError("lark outbound action requires conversation context")
    context = _base_context(action)
    context["lark_kind"] = action.kind
    if action.content_type != "text":
        context["content_type"] = action.content_type
    if action.card is not None:
        context["card"] = action.card
    attachment_source = _attachment_source(action)
    if attachment_source is not None:
        context["attachment"] = attachment_source
    return ChannelMessage(
        session_id=f"{conversation.channel_key}:{conversation.chat_id}",
        channel=conversation.channel_key,
        chat_id=conversation.chat_id,
        content=action.text or "",
        context=context,
        output_channel=conversation.channel_key,
        account_id=conversation.account_id,
        message_id=action.message_id,
        conversation=conversation,
        reply_grant=action.reply_grant,
        attachments=action.attachments,
        metadata=dict(action.metadata),
    )


def _wecom_message_from_action(action: OutboundAction) -> ChannelMessage:
    conversation = action.conversation
    if conversation is None:
        raise ValueError("wecom outbound action requires conversation context")
    context = _base_context(action)
    context["wecom_kind"] = action.kind
    if action.content_type != "text":
        context["content_type"] = action.content_type
    if action.card is not None:
        context["card"] = action.card
    if action.reply_grant is not None and action.reply_grant.token is not None:
        context["wecom_reply_token"] = action.reply_grant.token
        if action.reply_grant.reply_to_message_id is not None:
            context["wecom_reply_to_message_id"] = action.reply_grant.reply_to_message_id
        response_url = action.reply_grant.metadata.get("response_url")
        event_type = action.reply_grant.metadata.get("event_type")
        raw_msgtype = action.reply_grant.metadata.get("raw_msgtype")
        if response_url is not None:
            context["wecom_response_url"] = response_url
        if event_type is not None:
            context["wecom_event_type"] = event_type
        if raw_msgtype is not None:
            context["wecom_raw_msgtype"] = raw_msgtype
    return ChannelMessage(
        session_id=f"{conversation.channel_key}:{conversation.chat_id}",
        channel=conversation.channel_key,
        chat_id=conversation.chat_id,
        content=action.text or "",
        context=context,
        output_channel=conversation.channel_key,
        account_id=conversation.account_id,
        message_id=action.message_id,
        conversation=conversation,
        reply_grant=action.reply_grant,
        attachments=action.attachments,
        metadata={
            **dict(action.metadata),
            "mentions": [mention.as_dict() for mention in action.mentions],
            "target_ids": list(action.target_ids),
        },
    )


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
