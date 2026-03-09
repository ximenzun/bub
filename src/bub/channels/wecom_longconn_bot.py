from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub.channels.bridge import BridgeChannel, split_command
from bub.social import ChannelCapabilities, ContentConstraint, CredentialSpec, ProvisioningInfo
from bub.types import MessageHandler


class WeComLongConnBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_WECOM_LONGCONN_", extra="ignore", env_file=".env")

    command: str = Field(default="", description="Subprocess command that runs the WeCom long-connection bridge.")
    bot_id: str = Field(default="", description="WeCom smart bot Bot ID.")
    secret: str = Field(default="", description="WeCom smart bot Secret.")
    pairing_code: str = Field(default="", description="Optional interactive pairing code.")
    config_key: str = Field(default="", description="Optional config key returned during pairing.")


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
        return split_command(self._settings.command)
