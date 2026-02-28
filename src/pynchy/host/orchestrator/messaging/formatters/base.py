"""Base formatter protocol and rendered message type."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pynchy.types import OutboundEvent


@dataclass
class RenderedMessage:
    """Output of a formatter -- what gets sent to the channel transport."""

    text: str
    blocks: list[dict] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseFormatter(ABC):
    """Abstract base for channel message formatters."""

    @abstractmethod
    def render(self, event: OutboundEvent) -> RenderedMessage:
        """Convert an outbound event into a channel-ready message."""
        ...

    @abstractmethod
    def render_batch(self, events: list[OutboundEvent]) -> RenderedMessage:
        """Render multiple events as a single message (for trace batching)."""
        ...
