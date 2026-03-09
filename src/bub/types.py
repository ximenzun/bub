"""Framework-neutral data aliases."""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from bub.social import OutboundAction

type Envelope = Any
type State = dict[str, Any]
type MessageHandler = Callable[[Envelope], Coroutine[Any, Any, None]]
type OutboundDispatcher = Callable[[OutboundAction], Coroutine[Any, Any, bool]]
type ModelEventKind = Literal["text_delta", "action"]


class OutboundChannelRouter(Protocol):
    async def dispatch(self, action: OutboundAction) -> bool: ...


@dataclass(frozen=True)
class ModelEvent:
    """One event emitted by the model runtime."""

    kind: ModelEventKind
    text: str = ""
    action: OutboundAction | None = None


type ModelStream = AsyncIterable[ModelEvent]


@dataclass(frozen=True)
class TurnResult:
    """Result of one complete message turn."""

    session_id: str
    prompt: str
    model_output: str
    outbound_actions: list[OutboundAction] = field(default_factory=list)
