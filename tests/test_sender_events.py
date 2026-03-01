"""Tests for the OutboundEvent-based sender pipeline.

Verifies that broadcast() and finalize_stream_or_broadcast() work with
OutboundEvent objects instead of raw text strings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
from pynchy.types import OutboundEvent, OutboundEventType


def _make_channel(name: str, jid_prefix: str = "slack:"):
    ch = MagicMock()
    ch.name = name
    ch.is_connected.return_value = True
    ch.owns_jid.side_effect = lambda j: j.startswith(jid_prefix)
    ch.formatter = TextFormatter()
    ch.send_event = AsyncMock()
    return ch


def _make_deps(channels):
    deps = MagicMock()
    type(deps).channels = PropertyMock(return_value=channels)
    type(deps).workspaces = PropertyMock(return_value={})
    return deps


# ---------------------------------------------------------------------------
# broadcast() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_sends_event_to_channels():
    from pynchy.host.orchestrator.messaging.sender import broadcast

    ch = _make_channel("slack")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event)
    ch.send_event.assert_called_once_with("slack:C123", event)


@pytest.mark.asyncio
async def test_broadcast_skips_disconnected_channels():
    from pynchy.host.orchestrator.messaging.sender import broadcast

    ch = _make_channel("slack")
    ch.is_connected.return_value = False
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event)
    ch.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_skips_channel_that_does_not_own_jid():
    from pynchy.host.orchestrator.messaging.sender import broadcast

    ch = _make_channel("whatsapp", jid_prefix="wa:")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event)
    ch.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_skip_channel_parameter():
    from pynchy.host.orchestrator.messaging.sender import broadcast

    ch1 = _make_channel("slack")
    ch2 = _make_channel("slack2")
    ch2.owns_jid.side_effect = lambda j: j.startswith("slack:")
    deps = _make_deps([ch1, ch2])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event, skip_channel="slack")
    ch1.send_event.assert_not_called()
    ch2.send_event.assert_called_once_with("slack:C123", event)


# ---------------------------------------------------------------------------
# finalize_stream_or_broadcast() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_no_stream_falls_back_to_broadcast():
    from pynchy.host.orchestrator.messaging.sender import finalize_stream_or_broadcast

    ch = _make_channel("slack")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="done")
    await finalize_stream_or_broadcast(deps, "slack:C123", event, None)
    ch.send_event.assert_called_once_with("slack:C123", event)


@pytest.mark.asyncio
async def test_finalize_with_stream_updates_event():
    from pynchy.host.orchestrator.messaging.sender import finalize_stream_or_broadcast

    ch = _make_channel("slack")
    ch.update_event = AsyncMock()
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="final result")
    await finalize_stream_or_broadcast(deps, "slack:C123", event, {"slack": "msg-123"})
    ch.update_event.assert_called_once_with("slack:C123", "msg-123", event)
    ch.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_stream_update_failure_falls_back_to_send():
    from pynchy.host.orchestrator.messaging.sender import finalize_stream_or_broadcast

    ch = _make_channel("slack")
    ch.update_event = AsyncMock(side_effect=Exception("update failed"))
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="final result")
    await finalize_stream_or_broadcast(deps, "slack:C123", event, {"slack": "msg-123"})
    # Should fall back to send_event after update_event fails
    ch.send_event.assert_called_once_with("slack:C123", event)


# ---------------------------------------------------------------------------
# broadcast_formatted is removed
# ---------------------------------------------------------------------------


def test_broadcast_formatted_is_removed():
    """broadcast_formatted should no longer exist in sender.py."""
    from pynchy.host.orchestrator.messaging import sender

    assert not hasattr(sender, "broadcast_formatted"), (
        "broadcast_formatted should be removed — callers construct OutboundEvent directly"
    )
