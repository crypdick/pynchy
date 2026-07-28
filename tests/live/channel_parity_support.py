"""Channel parity tests for the built-in messaging channels.

These tests synthesize various message types (agent results, host messages, tool
traces, system events, etc.), push them through the common broadcasting code
paths, and verify that all channels receive equivalent output.

"Equivalent" accounts for known, intentional differences:
- Slack omits the assistant name prefix (the platform shows bot identity)
- WhatsApp prefixes agent messages with the assistant name
- Streaming channels receive updates via post_event/update_event

Run with:
    uv run pytest tests/live/ -m "live and parity"
    uv run pytest tests/live/ -m live
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)

if TYPE_CHECKING:
    from .conftest import (
        RecordingChannel,
    )

pytestmark = [pytest.mark.live, pytest.mark.parity]

CHAT_JID = "group@g.us"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_prefix(text: str) -> str:
    """Strip the emoji prefix if present, for comparing content parity.

    This normalizes '🦞 Hello world' → 'Hello world' so we can compare
    the actual content across channels that differ in prefix behavior.
    """
    prefix = "🦞 "
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def _normalize_messages(channel: RecordingChannel) -> list[str]:
    """Get normalized message texts from a channel for parity comparison.

    Strips the emoji prefix (since that's a known channel-specific
    difference), so the underlying content can be compared directly.
    """
    return [_strip_prefix(text) for _, text in channel.sent_messages]


def _text_event(text: str) -> OutboundEvent:
    """Wrap a string in a TEXT OutboundEvent for broadcast tests."""
    return OutboundEvent(type=OutboundEventType.TEXT, content=text)


def _make_deps(channels: list[RecordingChannel]) -> Any:
    """Create a mock ChannelDeps with the given channels."""
    deps = MagicMock()
    deps.channels = channels
    deps.event_bus = MagicMock()
    return deps
