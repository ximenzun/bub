import asyncio
from abc import ABC, abstractmethod
from typing import ClassVar

from bub.social import ChannelCapabilities, OutboundAction, basic_channel_capabilities


class Channel(ABC):
    """Base class for all channels"""

    name: ClassVar[str] = "base"

    @abstractmethod
    async def start(self, stop_event: asyncio.Event) -> None:
        """Start listening for events and dispatching to handlers."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the channel and clean up resources."""

    @property
    def needs_debounce(self) -> bool:
        """Whether this channel needs debounce to prevent overload. Default to False."""
        return False

    @property
    def capabilities(self) -> ChannelCapabilities:
        """Structured capability metadata for richer social-channel integrations."""
        return basic_channel_capabilities(self.name)

    async def send(self, action: OutboundAction) -> None:
        """Send a message to the channel. Optional to implement."""
        # Do nothing by default
        return
