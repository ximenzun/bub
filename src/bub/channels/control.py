from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from bub.social.capabilities import ChannelCapabilities, basic_channel_capabilities


@dataclass(frozen=True, slots=True)
class ChannelAccountStatus:
    channel: str
    account_id: str = "default"
    configured: bool = True
    running: bool | None = None
    state: str = "unknown"
    detail: str | None = None
    last_error: str | None = None
    last_event_at: str | None = None
    last_inbound_at: str | None = None
    last_outbound_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelLoginRequest:
    account_id: str | None = None
    force: bool = False
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ChannelLoginResult:
    channel: str
    account_id: str | None = None
    changed: bool = True
    lines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChannelControl:
    channel: str
    summary: str = ""
    capabilities: ChannelCapabilities = field(default_factory=lambda: basic_channel_capabilities("unknown"))
    status_handler: Callable[[], list[ChannelAccountStatus]] | None = None
    login_handler: Callable[[ChannelLoginRequest], ChannelLoginResult] | None = None
    logout_handler: Callable[[str | None, bool], list[str]] | None = None

    def status(self) -> list[ChannelAccountStatus]:
        if self.status_handler is None:
            return []
        return list(self.status_handler())

    def login(self, request: ChannelLoginRequest) -> ChannelLoginResult:
        if self.login_handler is None:
            raise NotImplementedError(f"Channel '{self.channel}' does not support login.")
        return self.login_handler(request)

    def logout(self, account_id: str | None = None, force: bool = False) -> list[str]:
        if self.logout_handler is None:
            raise NotImplementedError(f"Channel '{self.channel}' does not support logout.")
        return list(self.logout_handler(account_id, force))
