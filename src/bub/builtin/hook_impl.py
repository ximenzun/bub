import asyncio
import base64
import mimetypes
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import aiohttp
import typer
from loguru import logger
from republic.tape import TapeStore

from bub.builtin.agent import Agent
from bub.channels.base import Channel
from bub.channels.message import ChannelMessage, MediaItem, MessageKind
from bub.envelope import content_of, field_of
from bub.framework import BubFramework
from bub.hookspecs import hookimpl
from bub.social.types import Attachment
from bub.types import Envelope, MessageHandler, State
from bub.utils import workspace_from_state

AGENTS_FILE_NAME = "AGENTS.md"
IMAGE_REFERENCE_RE = re.compile(
    r"(上(图|面图|面图片|张图|张图片)|这(图|张图|张图片)|图片内容|图里|截图|看图|识别图片|分析图片|ocr|image above|picture above|this image|the image|screenshot)",
    re.IGNORECASE,
)
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
        quoted = await _quoted_prompt_context(self.agent, message=message, session_id=session_id, state=state)

        media = field_of(message, "media") or []
        attachments = field_of(message, "attachments") or []
        state.pop("_inbound_media_parts", None)
        state.pop("_inbound_media_refs", None)
        if not media and not attachments:
            if _should_restore_recent_image(content):
                recent_refs = await _recent_image_refs(self.agent, session_id=session_id, state=state)
                restored_parts = await _image_parts_from_refs(recent_refs)
                if restored_parts:
                    state["_inbound_media_parts"] = _clone_image_parts(restored_parts)
                    state["_inbound_media_refs"] = _clone_media_refs(recent_refs)
                    return _prompt_with_quote(text, current_image_parts=restored_parts, quoted=quoted)
            return _prompt_with_quote(text, current_image_parts=[], quoted=quoted)

        media_parts = await _image_parts_from_media(cast("list[MediaItem]", media))
        if not media_parts:
            media_parts = await _image_parts_from_attachments(_non_quoted_attachments(cast("list[Attachment]", attachments)))
        media_refs = _message_media_refs(message)
        if media_parts:
            if media_refs:
                state["_inbound_media_refs"] = _clone_media_refs(media_refs)
            if _is_image_only_content(content):
                text = (
                    f"{text}\n\nThe user sent an image without additional text. "
                    "Describe the image briefly and ask what they want next."
                )
            state["_inbound_media_parts"] = _clone_image_parts(media_parts)
            return _prompt_with_quote(text, current_image_parts=media_parts, quoted=quoted)
        return _prompt_with_quote(text, current_image_parts=[], quoted=quoted)

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


async def _image_parts_from_media(media: list[MediaItem]) -> list[dict[str, object]]:
    media_parts: list[dict[str, object]] = []
    for item in media:
        if item.type != "image" or item.data_fetcher is None:
            continue
        data = await item.data_fetcher()
        media_parts.append(_image_url_part(item.mime_type, data))
    return media_parts


async def _image_parts_from_attachments(attachments: list[Attachment]) -> list[dict[str, object]]:
    media_parts: list[dict[str, object]] = []
    for attachment in attachments:
        if not attachment.content_type.startswith("image/"):
            continue
        source = _attachment_source(attachment)
        if source is None:
            continue
        data_url = await _attachment_data_url(attachment.content_type, source)
        if data_url is not None:
            media_parts.append({"type": "image_url", "image_url": {"url": data_url}})
    return media_parts


def _image_url_part(mime_type: str, data: bytes) -> dict[str, object]:
    data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('utf-8')}"
    return {"type": "image_url", "image_url": {"url": data_url}}


def _clone_image_parts(parts: list[dict[str, object]]) -> list[dict[str, object]]:
    clones: list[dict[str, object]] = []
    for part in parts:
        image_url = part.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else None
        if part.get("type") != "image_url" or not isinstance(url, str) or not url:
            continue
        clones.append({"type": "image_url", "image_url": {"url": url}})
    return clones


def _clone_media_refs(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    return [dict(ref) for ref in refs]


def _message_media_refs(message: ChannelMessage) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for attachment in message.attachments:
        if _attachment_scope(attachment) == "quote":
            continue
        ref = _media_ref_from_attachment(attachment, channel=message.channel)
        if ref is not None:
            refs.append(ref)
    return refs


def _attachment_scope(attachment: Attachment) -> str:
    metadata = attachment.metadata if isinstance(attachment.metadata, dict) else {}
    scope = metadata.get("bub_scope")
    return scope if isinstance(scope, str) else "message"


def _non_quoted_attachments(attachments: list[Attachment]) -> list[Attachment]:
    return [attachment for attachment in attachments if _attachment_scope(attachment) != "quote"]


def _media_ref_from_attachment(attachment: Attachment, *, channel: str) -> dict[str, str] | None:
    if not attachment.content_type.startswith("image/"):
        return None
    ref: dict[str, str] = {"channel": channel, "content_type": attachment.content_type}
    if attachment.name:
        ref["name"] = attachment.name
    if attachment.url:
        ref["url"] = attachment.url
        return ref
    if attachment.file_key:
        ref["file_key"] = attachment.file_key
    metadata = attachment.metadata if isinstance(attachment.metadata, dict) else {}
    for key in ("message_id", "resource_type"):
        value = metadata.get(key)
        if isinstance(value, str) and value:
            ref[key] = value
    if {"message_id", "file_key", "resource_type"} <= set(ref):
        return ref
    return None


def _should_restore_recent_image(content: str) -> bool:
    return bool(IMAGE_REFERENCE_RE.search(content.strip()))


def _is_image_only_content(content: str) -> bool:
    normalized = content.strip().lower()
    return normalized in {"[lark image]", "[image]", "[telegram photo]", "[telegram image]", "[wecom image]"}


async def _recent_image_refs(agent: Agent, *, session_id: str, state: State) -> list[dict[str, str]]:
    tape = agent.tapes.session_tape(session_id, workspace_from_state(state))
    entries = list(await tape.query_async.all())
    for entry in reversed(entries):
        if entry.kind == "anchor":
            break
        if entry.kind != "message":
            continue
        payload = entry.payload
        if not isinstance(payload, dict) or payload.get("role") != "user":
            continue
        refs = payload.get("_bub_media_refs")
        if isinstance(refs, list):
            coerced = _coerce_media_refs(refs)
            if coerced:
                return coerced
    return []


def _coerce_media_refs(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    refs: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        channel = item.get("channel")
        if not isinstance(channel, str) or not channel:
            continue
        ref: dict[str, str] = {"channel": channel}
        for key in ("message_id", "file_key", "resource_type", "content_type", "url", "name"):
            value = item.get(key)
            if isinstance(value, str) and value:
                ref[key] = value
        if "url" in ref or {"message_id", "file_key", "resource_type"} <= set(ref):
            refs.append(ref)
    return refs


async def _image_parts_from_refs(refs: list[dict[str, str]]) -> list[dict[str, object]]:
    parts: list[dict[str, object]] = []
    for ref in refs:
        part = await _image_part_from_ref(ref)
        if part is not None:
            parts.append(part)
    return parts


async def _image_part_from_ref(ref: dict[str, str]) -> dict[str, object] | None:
    if ref.get("channel") != "lark":
        url = ref.get("url")
        content_type = ref.get("content_type") or "image/*"
        if isinstance(url, str):
            data_url = await _attachment_data_url(content_type, url)
            if data_url is not None:
                return {"type": "image_url", "image_url": {"url": data_url}}
        return None
    if ref.get("resource_type") != "image":
        return None
    message_id = ref.get("message_id")
    file_key = ref.get("file_key")
    if not message_id or not file_key:
        return None
    return await asyncio.to_thread(_fetch_lark_image_part_sync, ref, message_id, file_key)


def _fetch_lark_image_part_sync(ref: dict[str, str], message_id: str, file_key: str) -> dict[str, object] | None:
    try:
        from bub_lark.tools.common import coerce_binary_payload, ensure_lark_success, get_lark_client, get_lark_settings
        from lark_oapi.api.im.v1.model.get_message_resource_request import GetMessageResourceRequest
    except ImportError:
        return None

    settings = get_lark_settings()
    request = (
        GetMessageResourceRequest.builder()
        .message_id(message_id)
        .file_key(file_key)
        .type("image")
        .build()
    )
    response = get_lark_client().im.v1.message_resource.get(request)
    ensure_lark_success(response, "im.fetch_resource")
    payload = getattr(response, "file", None)
    if payload is None:
        raw = getattr(response, "raw", None)
        payload = getattr(raw, "content", None) if raw is not None else None
    if payload is None:
        return None
    content = coerce_binary_payload(payload)
    if len(content) > settings.download_max_bytes:
        return None
    raw = getattr(response, "raw", None)
    headers = getattr(raw, "headers", {}) if raw is not None else {}
    mime_type = headers.get("Content-Type") if isinstance(headers, dict) else None
    return _image_url_part(mime_type or ref.get("content_type") or "image/jpeg", content)


async def _quoted_prompt_context(
    agent: Agent,
    *,
    message: ChannelMessage,
    session_id: str,
    state: State,
) -> dict[str, object] | None:
    quoted = _quoted_from_metadata(message.metadata)
    if quoted is None:
        quoted = await _quoted_from_state(agent, session_id=session_id, state=state)
    if quoted is None:
        return None
    text = quoted.get("text")
    refs = _coerce_media_refs(quoted.get("media_refs"))
    image_parts = await _image_parts_from_refs(refs)
    if not isinstance(text, str) or not text.strip():
        text = "[quoted image]" if image_parts else ""
    if not text and not image_parts:
        return None
    return {"text": text, "image_parts": image_parts}


def _quoted_from_metadata(metadata: Mapping[str, object] | None) -> dict[str, object] | None:
    if not isinstance(metadata, Mapping):
        return None
    quoted = metadata.get("quoted_message")
    if not isinstance(quoted, Mapping):
        return None
    refs: list[dict[str, str]] = []
    raw_attachments = quoted.get("attachments")
    if isinstance(raw_attachments, list):
        for attachment in raw_attachments:
            if isinstance(attachment, Attachment):
                current_attachment = attachment
            elif isinstance(attachment, Mapping):
                current_attachment = Attachment.from_mapping(attachment)
            else:
                continue
            ref = _media_ref_from_attachment(current_attachment, channel=str(quoted.get("channel") or "unknown"))
            if ref is not None:
                refs.append(ref)
    return {
        "text": str(quoted.get("text") or "").strip(),
        "media_refs": refs,
    }


async def _quoted_from_state(agent: Agent, *, session_id: str, state: State) -> dict[str, object] | None:
    quote_message_id = _quoted_message_id_from_state(state)
    if quote_message_id is None:
        return None
    quoted = await _quoted_from_tape(agent, session_id=session_id, state=state, message_id=quote_message_id)
    if quoted is not None:
        return quoted
    if state.get("_inbound_channel") == "lark":
        return await asyncio.to_thread(_fetch_lark_quoted_message_sync, quote_message_id)
    return None


def _quoted_message_id_from_state(state: State) -> str | None:
    for key in ("_lark_parent_id", "_wecom_reply_to_message_id"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return value
    inbound_context = state.get("_inbound_context")
    if isinstance(inbound_context, Mapping):
        for key in ("parent_id", "reply_to_message_id"):
            value = inbound_context.get(key)
            if isinstance(value, str) and value:
                return value
    return None


async def _quoted_from_tape(agent: Agent, *, session_id: str, state: State, message_id: str) -> dict[str, object] | None:
    tape = agent.tapes.session_tape(session_id, workspace_from_state(state))
    entries = list(await tape.query_async.all())
    for entry in reversed(entries):
        if entry.kind == "anchor":
            break
        if entry.kind != "message":
            continue
        payload = entry.payload
        if not isinstance(payload, Mapping):
            continue
        if payload.get("_bub_inbound_message_id") != message_id:
            continue
        content = payload.get("content")
        return {
            "text": _strip_context_prefix(content) if isinstance(content, str) else "",
            "media_refs": _coerce_media_refs(payload.get("_bub_media_refs")),
        }
    return None


def _strip_context_prefix(content: str) -> str:
    _prefix, separator, remainder = content.partition("\n---\n")
    if separator:
        return remainder
    return content


def _fetch_lark_quoted_message_sync(message_id: str) -> dict[str, object] | None:
    try:
        from bub_lark.channel import (
            _attachment_mime_for_message_type,
            _field,
            _flatten_post_content,
            _message_content,
            _post_resource_descriptors,
            _resource_type_for_message_type,
            _share_summary,
            _summarize_interactive_card,
        )
        from bub_lark.tools.common import ensure_lark_success, get_lark_client, response_data
        from lark_oapi.api.im.v1.model.get_message_request import GetMessageRequest
    except ImportError:
        return None

    request = GetMessageRequest.builder().message_id(message_id).build()
    request.add_query("card_msg_content_type", "raw_card_content")
    response = get_lark_client().im.v1.message.get(request)
    ensure_lark_success(response, "im.get_message")
    data = response_data(response)
    message = getattr(data, "message", None)
    if message is None:
        items = getattr(data, "items", None)
        if isinstance(items, list) and items:
            message = items[0]
    if message is None:
        return None
    body = _field(message, "body")
    raw_content = _field(body, "content", _field(message, "content", ""))
    _raw, content = _message_content(raw_content)
    message_type = str(_field(message, "msg_type") or _field(message, "message_type") or "unknown")
    text, refs = _parse_lark_quoted_content(
        message_id=message_id,
        message_type=message_type,
        content=content,
        raw_content=raw_content,
        flatten_post=_flatten_post_content,
        post_resource_descriptors=_post_resource_descriptors,
        attachment_mime_for_message_type=_attachment_mime_for_message_type,
        resource_type_for_message_type=_resource_type_for_message_type,
        summarize_interactive_card=_summarize_interactive_card,
        share_summary=_share_summary,
    )
    if not text and not refs:
        return None
    return {"text": text, "media_refs": refs}


def _parse_lark_quoted_content(
    *,
    message_id: str,
    message_type: str,
    content: object,
    raw_content: object,
    flatten_post,
    post_resource_descriptors,
    attachment_mime_for_message_type,
    resource_type_for_message_type,
    summarize_interactive_card,
    share_summary,
) -> tuple[str, list[dict[str, str]]]:
    if message_type == "text":
        if isinstance(content, dict):
            return str(content.get("text") or "").strip(), []
        return str(raw_content or "").strip(), []
    if message_type == "post":
        return _parse_lark_post_quoted_content(
            message_id=message_id,
            content=content,
            flatten_post=flatten_post,
            post_resource_descriptors=post_resource_descriptors,
            attachment_mime_for_message_type=attachment_mime_for_message_type,
        )
    if message_type in {"image", "file", "audio", "media", "sticker"} and isinstance(content, dict):
        refs = _lark_media_refs_from_content(
            message_id=message_id,
            message_type=message_type,
            content=content,
            attachment_mime_for_message_type=attachment_mime_for_message_type,
            resource_type_for_message_type=resource_type_for_message_type,
        )
        return f"[Quoted Lark {message_type}]", refs
    if message_type == "interactive":
        return summarize_interactive_card(content), []
    if message_type in {"share_chat", "share_user"}:
        return share_summary(message_type, content), []
    return "", []


def _parse_lark_post_quoted_content(
    *,
    message_id: str,
    content: object,
    flatten_post,
    post_resource_descriptors,
    attachment_mime_for_message_type,
) -> tuple[str, list[dict[str, str]]]:
    if not isinstance(content, dict):
        return "[Lark post message]", []
    body_content = {"default": content} if "content" in content else content
    text = flatten_post(body_content).strip() or "[Lark post message]"
    refs: list[dict[str, str]] = []
    for resource_type, resource_content in post_resource_descriptors(body_content):
        file_key = str(resource_content.get("file_key") or resource_content.get("image_key") or "").strip()
        if not file_key:
            continue
        refs.append(
            {
                "channel": "lark",
                "message_id": message_id,
                "file_key": file_key,
                "resource_type": resource_type,
                "content_type": attachment_mime_for_message_type(resource_type),
            }
        )
    return text, refs


def _lark_media_refs_from_content(
    *,
    message_id: str,
    message_type: str,
    content: Mapping[str, object],
    attachment_mime_for_message_type,
    resource_type_for_message_type,
) -> list[dict[str, str]]:
    file_key = str(content.get("file_key") or content.get("image_key") or "").strip()
    if not file_key:
        return []
    return [
        {
            "channel": "lark",
            "message_id": message_id,
            "file_key": file_key,
            "resource_type": resource_type_for_message_type(message_type),
            "content_type": attachment_mime_for_message_type(message_type),
        }
    ]


def _prompt_with_quote(
    current_text: str,
    *,
    current_image_parts: list[dict[str, object]],
    quoted: dict[str, object] | None,
) -> str | list[dict]:
    quoted_text = quoted.get("text") if isinstance(quoted, dict) else None
    quoted_image_parts = quoted.get("image_parts") if isinstance(quoted, dict) else None
    quote_images = quoted_image_parts if isinstance(quoted_image_parts, list) else []
    quote_text = quoted_text if isinstance(quoted_text, str) else ""
    if not quote_images and not current_image_parts:
        if not quote_text:
            return current_text
        return f"Quoted message:\n{quote_text}\n\nCurrent message:\n{current_text}"

    parts: list[dict[str, object]] = []
    if quote_text or quote_images:
        parts.append({"type": "text", "text": f"Quoted message:\n{quote_text or '[quoted image]'}"})
        parts.extend(quote_images)
        parts.append({"type": "text", "text": f"Current message:\n{current_text}"})
    else:
        parts.append({"type": "text", "text": current_text})
    parts.extend(current_image_parts)
    return parts


def _attachment_source(attachment: Attachment) -> str | None:
    if attachment.url:
        return attachment.url
    path = attachment.metadata.get("path")
    if path is None:
        return None
    return str(path)


async def _attachment_data_url(content_type: str, source: str) -> str | None:
    if source.startswith("data:"):
        return source
    if source.startswith("http://") or source.startswith("https://"):
        async with aiohttp.ClientSession() as session, session.get(source) as response:
            response.raise_for_status()
            data = await response.read()
            detected_type = response.headers.get("Content-Type")
        return _data_url(_resolved_content_type(content_type, detected_type), data)
    path = Path(source.removeprefix("file://")).expanduser()
    if not path.is_file():
        return None
    data = await asyncio.to_thread(path.read_bytes)
    guessed_type, _encoding = mimetypes.guess_type(path.name)
    return _data_url(_resolved_content_type(content_type, guessed_type), data)


def _data_url(content_type: str, data: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode('utf-8')}"


def _resolved_content_type(content_type: str, detected_type: str | None) -> str:
    if content_type != "image/*":
        return content_type
    if isinstance(detected_type, str) and detected_type:
        return detected_type
    return "image/png"
