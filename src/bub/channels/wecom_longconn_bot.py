from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from bub.channels.bridge import BridgeChannel, run_command, split_command
from bub.channels.bridge_protocol import build_configure_frame
from bub.social import ActionConstraint, ChannelCapabilities, ContentConstraint, CredentialSpec, ProvisioningInfo
from bub.types import MessageHandler


class WeComLongConnBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BUB_WECOM_LONGCONN_", extra="ignore", env_file=".env")

    command: str = Field(default="", description="Subprocess command that runs the WeCom long-connection bridge.")
    node_command: str = Field(default="node", description="Node.js executable used for the bundled WeCom bridge.")
    npm_command: str = Field(default="npm", description="npm executable used to bootstrap the bundled WeCom bridge.")
    bot_id: str = Field(default="", description="WeCom smart bot Bot ID.")
    secret: str = Field(default="", description="WeCom smart bot Secret.")
    pairing_code: str = Field(default="", description="Optional interactive pairing code.")
    config_key: str = Field(default="", description="Optional config key returned during pairing.")
    callback_token: str = Field(default="", description="Optional callback verification token for event decrypt.")
    encoding_aes_key: str = Field(default="", description="Optional callback AES key for event decrypt.")
    websocket_url: str = Field(default="", description="Optional WeCom WebSocket URL override.")
    mock: bool = Field(default=False, description="Run the bundled bridge in mock mode.")
    ready_timeout_seconds: float = Field(default=5.0, description="Seconds to wait for the bridge ready frame.")


class WeComLongConnBotChannel(BridgeChannel):
    """Enterprise WeCom smart-bot adapter via subprocess bridge."""

    name = "wecom_longconn_bot"

    def __init__(self, on_receive: MessageHandler) -> None:
        super().__init__(on_receive=on_receive)
        self._settings = WeComLongConnBotSettings()

    @property
    def capabilities(self) -> ChannelCapabilities:
        state = "active" if self.command else "pending"
        return ChannelCapabilities(
            platform="wecom",
            adapter_mode="bridge",
            transport="long_connection",
            provisioning_mode="interactive_pairing",
            supported_actions=frozenset({"send_message", "reply_message", "update_card"}),
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
            constraints={
                "send_message": ActionConstraint(notes=("proactive sends support markdown and template cards only",)),
                "reply_message": ActionConstraint(notes=("passive replies support text, markdown, template cards, and image replies",)),
                "update_card": ActionConstraint(
                    max_age_seconds=5,
                    notes=("requires a reply_grant token from a WeCom template_card_event callback",),
                ),
            },
            content_constraints={
                "text": ContentConstraint(max_body_bytes=2048, supports_mentions=True),
                "rich_text": ContentConstraint(max_body_bytes=4096, supports_mentions=True),
                "image": ContentConstraint(notes=("reply-only",)),
                "audio": ContentConstraint(notes=("not supported for outbound long-connection replies",)),
                "file": ContentConstraint(notes=("not supported for outbound long-connection replies",)),
                "card": ContentConstraint(notes=("template_card",)),
            },
        )

    @property
    def command(self) -> Sequence[str]:
        if explicit := split_command(self._settings.command):
            return explicit
        if self._settings.bot_id and self._settings.secret:
            command = [self._settings.node_command, str(self._bridge_script_path()), "--channel", self.name]
            if self._settings.mock:
                command.append("--mock")
            return command
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
            "websocket_url": self._settings.websocket_url or None,
        }

    async def prepare(self) -> None:
        if split_command(self._settings.command):
            return
        if not (self._settings.bot_id and self._settings.secret):
            return
        if self._settings.mock:
            return
        package_json = self._bridge_runtime_dir() / "package.json"
        sdk_package = self._bridge_runtime_dir() / "node_modules" / "@wecom" / "aibot-node-sdk" / "package.json"
        if not package_json.exists():
            raise RuntimeError(f"WeCom bridge runtime package.json not found at {package_json}")
        if sdk_package.exists():
            return
        await run_command([self._settings.npm_command, "install", "--no-fund", "--no-audit"], cwd=self._bridge_runtime_dir())

    @staticmethod
    def _bridge_runtime_dir() -> Path:
        return Path(__file__).with_name("node")

    @staticmethod
    def _bridge_script_path() -> Path:
        return WeComLongConnBotChannel._bridge_runtime_dir() / "wecom_longconn_bridge.mjs"
