from __future__ import annotations

import sys
from collections.abc import Sequence

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub.channels.bridge import BridgeChannel, split_command
from bub.channels.bridge_protocol import build_configure_frame
from bub.social import ChannelCapabilities, ContentConstraint, CredentialSpec, ProvisioningInfo
from bub.types import MessageHandler


class WeComLongConnBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_WECOM_LONGCONN_", extra="ignore", env_file=".env")

    command: str = Field(default="", description="Subprocess command that runs the WeCom long-connection bridge.")
    bot_id: str = Field(default="", description="WeCom smart bot Bot ID.")
    secret: str = Field(default="", description="WeCom smart bot Secret.")
    pairing_code: str = Field(default="", description="Optional interactive pairing code.")
    config_key: str = Field(default="", description="Optional config key returned during pairing.")
    callback_token: str = Field(default="", description="Optional callback verification token for event decrypt.")
    encoding_aes_key: str = Field(default="", description="Optional callback AES key for event decrypt.")
    ready_timeout_seconds: float = Field(default=5.0, description="Seconds to wait for the bridge ready frame.")


class WeComLongConnBotChannel(BridgeChannel):
    """Enterprise WeCom smart-bot adapter via subprocess bridge."""

    name = "wecom_longconn_bot"

    def __init__(self, on_receive: MessageHandler) -> None:
        super().__init__(on_receive=on_receive)
        self._settings = WeComLongConnBotSettings()

    @property
    def capabilities(self) -> ChannelCapabilities:
        state = "active" if self._settings.command else "pending"
        return ChannelCapabilities(
            platform="wecom",
            adapter_mode="bridge",
            transport="long_connection",
            provisioning_mode="interactive_pairing",
            supported_actions=frozenset({"send_message", "reply_message", "edit_message", "presence"}),
            supports_rich_text=True,
            supports_cards=True,
            supports_attachments=True,
            credential_specs=(
                CredentialSpec(key="bot_id", kind="bot_secret", secret=False, env_var="BUB_WECOM_LONGCONN_BOT_ID"),
                CredentialSpec(key="secret", kind="bot_secret", env_var="BUB_WECOM_LONGCONN_SECRET"),
                CredentialSpec(
                    key="callback_token",
                    kind="token",
                    secret=False,
                    required=False,
                    env_var="BUB_WECOM_LONGCONN_CALLBACK_TOKEN",
                ),
                CredentialSpec(
                    key="encoding_aes_key",
                    kind="custom",
                    required=False,
                    env_var="BUB_WECOM_LONGCONN_ENCODING_AES_KEY",
                ),
            ),
            provisioning=ProvisioningInfo(
                mode="interactive_pairing",
                state=state,  # type: ignore[arg-type]
                pairing_code=self._settings.pairing_code or None,
                config_key=self._settings.config_key or None,
            ),
            content_constraints={
                "text": ContentConstraint(max_body_bytes=2048, supports_mentions=True),
                "rich_text": ContentConstraint(max_body_bytes=4096, supports_mentions=True),
                "card": ContentConstraint(notes=("template_card",)),
            },
        )

    @property
    def command(self) -> Sequence[str]:
        if explicit := split_command(self._settings.command):
            return explicit
        if self._settings.bot_id and self._settings.secret:
            return [sys.executable, "-m", "bub.channels.wecom_longconn_bridge", "--channel", self.name]
        return []

    @property
    def ready_timeout_seconds(self) -> float:
        return self._settings.ready_timeout_seconds

    @property
    def startup_frames(self) -> list[dict[str, object]]:
        return [build_configure_frame(self.name, self.bridge_config)]

    @property
    def bridge_config(self) -> dict[str, object]:
        return {
            "bot_id": self._settings.bot_id,
            "secret": self._settings.secret,
            "pairing_code": self._settings.pairing_code or None,
            "config_key": self._settings.config_key or None,
            "callback_token": self._settings.callback_token or None,
            "encoding_aes_key": self._settings.encoding_aes_key or None,
        }
