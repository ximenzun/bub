"""Model backend protocol for Bub runtime integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from republic import LLM, AsyncTapeStore
    from republic.tape import TapeContext

    from bub.builtin.settings import AgentSettings


class ModelBackend(Protocol):
    """Build the runtime LLM object used by Bub's agent loop."""

    def build_llm(self, *, settings: AgentSettings, tape_store: AsyncTapeStore, context: TapeContext) -> LLM: ...
