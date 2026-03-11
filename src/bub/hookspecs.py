"""Pluggy hook namespace and framework hook specifications."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import pluggy
from republic import AsyncTapeStore
from republic.tape import TapeStore

from bub.social import OutboundAction
from bub.types import Envelope, MessageHandler, ModelEvent, ModelStream, PromptInput, State

if TYPE_CHECKING:
    from bub.channels.base import Channel
    from bub.commands import SlashCommandSpec
    from bub.model_backend import ModelBackend

BUB_HOOK_NAMESPACE = "bub"
hookspec = pluggy.HookspecMarker(BUB_HOOK_NAMESPACE)
hookimpl = pluggy.HookimplMarker(BUB_HOOK_NAMESPACE)


class BubHookSpecs:
    """Hook contract for Bub framework extensions."""

    @hookspec(firstresult=True)
    def resolve_session(self, message: Envelope) -> str:
        """Resolve session id for one inbound message."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def load_state(self, message: Envelope, session_id: str) -> State:
        """Load state snapshot for one session."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def build_prompt(self, message: Envelope, session_id: str, state: State) -> PromptInput:
        """Build model prompt for this turn."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def run_model_stream(self, prompt: PromptInput, session_id: str, state: State) -> ModelStream | Iterable[ModelEvent]:
        """Run model for one turn and emit model events."""
        raise NotImplementedError

    @hookspec
    def save_state(
        self,
        session_id: str,
        state: State,
        message: Envelope,
        model_output: str,
    ) -> None:
        """Persist state updates after one model turn."""

    @hookspec
    def render_actions(
        self,
        message: Envelope,
        session_id: str,
        state: State,
        model_output: str,
    ) -> list[OutboundAction]:
        """Render outbound actions from model output."""
        raise NotImplementedError

    @hookspec
    def dispatch_outbound(self, action: OutboundAction) -> bool:
        """Dispatch one outbound action to external channel(s)."""
        raise NotImplementedError

    @hookspec
    def register_cli_commands(self, app: Any) -> None:
        """Register CLI commands onto the root Typer application."""

    @hookspec
    def on_error(self, stage: str, error: Exception, message: Envelope | None) -> None:
        """Observe framework errors from any stage."""

    @hookspec
    def system_prompt(self, prompt: PromptInput, state: State) -> str:
        """Provide a system prompt to be prepended to all model prompts."""
        raise NotImplementedError

    @hookspec(firstresult=True)
    def provide_tape_store(self) -> TapeStore | AsyncTapeStore:
        """Provide a tape store instance for Bub's conversation recording feature."""
        ...

    @hookspec(firstresult=True)
    def provide_model_backend(self) -> ModelBackend:
        """Provide the model backend used to build Bub's runtime LLM client."""
        ...

    @hookspec
    def provide_slash_commands(self) -> list[SlashCommandSpec]:
        """Provide discoverable slash commands for chat channels."""
        raise NotImplementedError

    @hookspec
    def provide_channels(self, message_handler: MessageHandler) -> list[Channel]:
        """Provide a list of channels for receiving messages."""
        raise NotImplementedError
