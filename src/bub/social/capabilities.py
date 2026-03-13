"""Optional capability metadata for richer social-channel integrations."""

from __future__ import annotations

from dataclasses import dataclass, field

from bub.social.types import (
    ActionKind,
    AdapterMode,
    ContentKind,
    CredentialSpec,
    MentionTargetKind,
    ProgressSurface,
    ProvisioningInfo,
    ProvisioningMode,
    TransportKind,
)


@dataclass(slots=True)
class ActionConstraint:
    requires_ownership: bool = False
    rate_limit_qps: float | None = None
    max_age_seconds: int | None = None
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class ContentConstraint:
    max_body_bytes: int | None = None
    supports_mentions: bool = False
    notes: tuple[str, ...] = ()


@dataclass(slots=True)
class ChannelCapabilities:
    platform: str
    adapter_mode: AdapterMode = "native"
    transport: TransportKind = "unknown"
    provisioning_mode: ProvisioningMode = "none"
    supported_actions: frozenset[ActionKind] = field(default_factory=lambda: frozenset({"send_message"}))
    progress_surfaces: frozenset[ProgressSurface] = field(default_factory=frozenset)
    supports_rich_text: bool = False
    supports_cards: bool = False
    supports_attachments: bool = False
    mention_target_kinds: frozenset[MentionTargetKind] = field(default_factory=frozenset)
    credential_specs: tuple[CredentialSpec, ...] = ()
    provisioning: ProvisioningInfo = field(default_factory=ProvisioningInfo)
    constraints: dict[ActionKind, ActionConstraint] = field(default_factory=dict)
    content_constraints: dict[ContentKind, ContentConstraint] = field(default_factory=dict)


def basic_channel_capabilities(platform: str) -> ChannelCapabilities:
    return ChannelCapabilities(platform=platform)
