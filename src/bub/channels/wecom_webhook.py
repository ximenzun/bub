from __future__ import annotations

import asyncio
import base64
import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp
from loguru import logger
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub.channels.base import Channel
from bub.social import (
    ActionConstraint,
    Attachment,
    ChannelCapabilities,
    ContentConstraint,
    CredentialSpec,
    OutboundAction,
    ProvisioningInfo,
)


class WeComWebhookSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_WECOM_WEBHOOK_", extra="ignore", env_file=".env")

    webhook_url: str = Field(default="", description="WeCom webhook send URL.")
    timeout_seconds: float = Field(default=10.0, description="HTTP timeout in seconds for webhook requests.")


class WeComWebhookChannel(Channel):
    """Enterprise WeCom webhook adapter (outbound-only)."""

    name = "wecom_webhook"

    def __init__(self) -> None:
        self._settings = WeComWebhookSettings()
        self._session: aiohttp.ClientSession | None = None

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            platform="wecom",
            adapter_mode="webhook_sink",
            transport="webhook",
            provisioning_mode="static_config",
            supported_actions=frozenset({"send_message"}),
            supports_rich_text=True,
            supports_cards=True,
            supports_attachments=True,
            mention_target_kinds=frozenset({"user_id", "mobile", "all"}),
            credential_specs=(
                CredentialSpec(
                    key="webhook_url",
                    kind="webhook_url",
                    env_var="BUB_WECOM_WEBHOOK_URL",
                    description="WeCom webhook URL from the message-push configuration page.",
                    example="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx",
                ),
            ),
            provisioning=ProvisioningInfo(mode="static_config", state="active" if self._settings.webhook_url else "pending"),
            constraints={
                "send_message": ActionConstraint(
                    rate_limit_qps=20 / 60,
                    notes=("WeCom webhook is limited to 20 messages per minute per webhook.",),
                ),
            },
            content_constraints={
                "text": ContentConstraint(max_body_bytes=2048, supports_mentions=True),
                "rich_text": ContentConstraint(max_body_bytes=4096, supports_mentions=True),
                "image": ContentConstraint(max_body_bytes=2 * 1024 * 1024, notes=("raw image bytes",)),
                "file": ContentConstraint(notes=("requires upload_media first",)),
                "audio": ContentConstraint(notes=("voice upload uses upload_media?type=voice",)),
                "card": ContentConstraint(notes=("template_card payload",)),
            },
        )

    async def start(self, stop_event: asyncio.Event) -> None:
        logger.info("wecom_webhook.start configured={}", bool(self._settings.webhook_url))

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info("wecom_webhook.stopped")

    async def send(self, action: OutboundAction) -> None:
        if not self._settings.webhook_url:
            raise RuntimeError("WeCom webhook URL is not configured.")
        if action.kind == "update_card":
            raise RuntimeError("WeCom webhook does not support update_card.")
        if action.kind == "edit_message":
            raise RuntimeError("WeCom webhook does not support edit_message.")
        payload = await self._build_payload(action)
        await self._post_json(payload)

    async def _build_payload(self, action: OutboundAction) -> dict[str, object]:
        raw_payload = action.metadata.get("wecom_payload")
        if isinstance(raw_payload, dict):
            return raw_payload

        msgtype = self._infer_msgtype(action)
        if msgtype == "text":
            return self._build_text_payload(action)
        if msgtype in {"markdown", "markdown_v2"}:
            return self._build_markdown_payload(msgtype, action)
        if msgtype == "image":
            return await self._build_image_payload(action)
        if msgtype in {"file", "voice"}:
            return await self._build_uploaded_media_payload(msgtype, action)
        if msgtype == "template_card":
            return self._build_template_card_payload(action)
        raise ValueError(f"Unsupported WeCom webhook msgtype: {msgtype}")

    def _build_text_payload(self, action: OutboundAction) -> dict[str, object]:
        mentioned_list: list[str] = []
        mentioned_mobile_list: list[str] = []
        for mention in action.mentions:
            match mention.kind:
                case "user_id":
                    mentioned_list.append(mention.value)
                case "mobile":
                    mentioned_mobile_list.append(mention.value)
                case "all":
                    mentioned_list.append("@all")
                    mentioned_mobile_list.append("@all")
                case _:
                    continue
        text_payload: dict[str, object] = {"content": action.text or ""}
        if mentioned_list:
            text_payload["mentioned_list"] = mentioned_list
        if mentioned_mobile_list:
            text_payload["mentioned_mobile_list"] = mentioned_mobile_list
        return {"msgtype": "text", "text": text_payload}

    @staticmethod
    def _build_markdown_payload(msgtype: str, action: OutboundAction) -> dict[str, object]:
        return {"msgtype": msgtype, msgtype: {"content": action.text or ""}}

    async def _build_image_payload(self, action: OutboundAction) -> dict[str, object]:
        content, _filename, _mime_type = await self._read_binary_payload(action)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError("WeCom webhook images must be 2MB or smaller.")
        return {
            "msgtype": "image",
            "image": {
                "base64": base64.b64encode(content).decode("utf-8"),
                "md5": hashlib.md5(content, usedforsecurity=False).hexdigest(),
            },
        }

    async def _build_uploaded_media_payload(self, msgtype: str, action: OutboundAction) -> dict[str, object]:
        media_type = "voice" if msgtype == "voice" else "file"
        media_id = await self._upload_media(media_type, action)
        return {"msgtype": msgtype, msgtype: {"media_id": media_id}}

    @staticmethod
    def _build_news_payload(action: OutboundAction) -> dict[str, object]:
        articles = action.metadata.get("articles")
        if not isinstance(articles, list) or not articles:
            raise ValueError("WeCom news messages require metadata['articles'].")
        return {"msgtype": "news", "news": {"articles": articles}}

    @staticmethod
    def _build_template_card_payload(action: OutboundAction) -> dict[str, object]:
        template_card = action.card
        if not isinstance(template_card, dict):
            raise TypeError("WeCom template_card messages require action.card.")
        return {"msgtype": "template_card", "template_card": template_card}

    async def _upload_media(self, media_type: str, action: OutboundAction) -> str:
        content, filename, mime_type = await self._read_binary_payload(action)
        upload_url = self._upload_url(media_type)
        form = aiohttp.FormData()
        form.add_field(
            "media",
            content,
            filename=filename or f"upload.{self._default_extension(mime_type)}",
            content_type=mime_type or "application/octet-stream",
        )
        session = await self._session_or_create()
        async with session.post(upload_url, data=form) as response:
            response.raise_for_status()
            data = await response.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom media upload failed: {data}")
        media_id = data.get("media_id")
        if not isinstance(media_id, str) or not media_id:
            raise RuntimeError(f"WeCom media upload returned no media_id: {data}")
        return media_id

    async def _post_json(self, payload: dict[str, object]) -> None:
        session = await self._session_or_create()
        async with session.post(self._settings.webhook_url, json=payload) as response:
            response.raise_for_status()
            data = await response.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"WeCom webhook send failed: {data}")

    async def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._settings.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _read_binary_payload(self, action: OutboundAction) -> tuple[bytes, str | None, str | None]:
        if action.attachments:
            return await self._read_attachment(action.attachments[0])
        if action.text:
            return await self._read_source(action.text)
        raise ValueError("WeCom media actions require an attachment or a text source path/URL.")

    async def _read_attachment(self, attachment: Attachment) -> tuple[bytes, str | None, str | None]:
        source = attachment.url or attachment.metadata.get("path")
        if not source:
            raise ValueError("Attachment does not contain a readable source URL or path.")
        content, filename, detected_mime = await self._read_source(str(source))
        return content, attachment.name or filename, attachment.content_type or detected_mime

    async def _read_source(self, source: str) -> tuple[bytes, str | None, str | None]:
        if source.startswith("data:"):
            header, encoded = source.split(",", 1)
            mime_type = header[5:].split(";", 1)[0] if header.startswith("data:") else None
            return base64.b64decode(encoded), None, mime_type
        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            session = await self._session_or_create()
            async with session.get(source) as response:
                response.raise_for_status()
                content = await response.read()
                mime_type = response.headers.get("Content-Type")
            filename = Path(parsed.path).name or None
            return content, filename, mime_type
        path = Path(source.removeprefix("file://")).expanduser()
        content = path.read_bytes()
        mime_type, _encoding = mimetypes.guess_type(path.name)
        return content, path.name, mime_type

    def _upload_url(self, media_type: str) -> str:
        parsed = urlparse(self._settings.webhook_url)
        key = parse_qs(parsed.query).get("key", [""])[0]
        if not key:
            raise ValueError("WeCom webhook URL does not contain a 'key' query parameter.")
        return f"{parsed.scheme}://{parsed.netloc}/cgi-bin/webhook/upload_media?key={key}&type={media_type}"

    @staticmethod
    def _infer_msgtype(action: OutboundAction) -> str:
        if action.card is not None or action.content_type == "card":
            return "template_card"
        if action.content_type == "image":
            return "image"
        if action.content_type == "file":
            return "file"
        if action.content_type == "audio":
            return "voice"
        if action.content_type == "rich_text":
            return "markdown"
        return "text"

    @staticmethod
    def _default_extension(mime_type: str | None) -> str:
        if not mime_type:
            return "bin"
        guessed = mimetypes.guess_extension(mime_type)
        if not guessed:
            return "bin"
        return guessed.lstrip(".")
