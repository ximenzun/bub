from __future__ import annotations

import pytest

from bub.builtin.settings import AgentSettings
from bub.channels.manager import ChannelSettings
from bub.channels.telegram import TelegramSettings


def test_agent_settings_accepts_provider_maps(tmp_path) -> None:
    settings = AgentSettings(
        home=tmp_path / "home",
        api_key={"openai": "sk-openai", "anthropic": "sk-anthropic"},
        api_base={"openai": "https://api.openai.com"},
        model="openai:gpt-5",
    )

    assert settings.home == tmp_path / "home"
    assert settings.api_key["openai"] == "sk-openai"
    assert settings.api_base["openai"] == "https://api.openai.com"


def test_agent_settings_rejects_responses_api_with_chat_completions_base() -> None:
    with pytest.raises(ValueError, match="api_format=responses is incompatible"):
        AgentSettings(
            api_key="sk-test",
            api_base="http://127.0.0.1:20002/chat-completions",
            api_format="responses",
        )


def test_channel_settings_accepts_list_enabled_channels() -> None:
    settings = ChannelSettings(enabled_channels=["telegram", "lark"], debounce_seconds=2.0)

    assert settings.enabled_channels == ["telegram", "lark"]
    assert settings.debounce_seconds == 2.0


def test_telegram_settings_accepts_runtime_override_payload() -> None:
    token = "123:abc"  # noqa: S105
    settings = TelegramSettings(
        token=token,
        allow_users="alice",
        allow_chats="-1001",
        proxy="http://127.0.0.1:7890",
    )

    assert settings.token == token
    assert settings.allow_users == "alice"
    assert settings.allow_chats == "-1001"
    assert settings.proxy == "http://127.0.0.1:7890"
