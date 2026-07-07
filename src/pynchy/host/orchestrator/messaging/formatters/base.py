"""Base formatter protocol and rendered message type."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pynchy.types import OutboundEvent


@dataclass
class RenderedMessage:
    """Output of a formatter -- what gets sent to the channel transport."""

    text: str
    blocks: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Formatter(Protocol):
    """Structural interface for channel message formatters."""

    def render(self, event: OutboundEvent) -> RenderedMessage:
        """Convert an outbound event into a channel-ready message."""
        ...

    def render_batch(self, events: list[OutboundEvent]) -> RenderedMessage:
        """Render multiple events as a single message (for trace batching)."""
        ...
