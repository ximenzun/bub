"""Slash-command metadata shared across channels and plugins."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommandSpec:
    """One discoverable slash command exposed to chat channels."""

    name: str
    summary: str
    usage: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    topic: str | None = None

    def __post_init__(self) -> None:
        if not self.name.startswith("/"):
            raise ValueError("slash command names must start with '/'")

    @property
    def topic_key(self) -> str:
        return (self.topic or self.name.lstrip("/")).strip().casefold()
