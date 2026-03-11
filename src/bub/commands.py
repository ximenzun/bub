"""Slash-command metadata shared across channels and plugins."""

from __future__ import annotations

import shlex
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


def translate_registered_slash_command(content: str, commands: list[SlashCommandSpec]) -> str | None:
    stripped = content.strip()
    if not stripped.startswith("/"):
        return None
    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return None
    if not tokens:
        return None

    root = tokens[0].casefold()
    command_names = {command.name.casefold() for command in commands}
    if root in {"/commands", "/help"}:
        if len(tokens) == 2:
            return f",commands topic={tokens[1]}"
        return ",commands"
    if root in command_names and (len(tokens) == 1 or (len(tokens) == 2 and tokens[1].casefold() == "help")):
        return f",commands topic={root.lstrip('/')}"
    return None
