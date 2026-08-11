"""Tests for the OutboundEvent-based sender pipeline.

Verifies that broadcast() and finalize_stream_or_broadcast() work with
OutboundEvent objects instead of raw text strings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from pynchy.host.orchestrator.messaging import sender
from pynchy.host.orchestrator.messaging.formatters.text import TextFormatter
from pynchy.host.orchestrator.messaging.sender import (
    broadcast,
    finalize_stream_or_broadcast,
)
from pynchy.plugins.api import (
    Channel,
    OutboundEvent,
    OutboundEventType,
)


def _make_channel(name: str, jid_prefix: str = "slack:"):
    ch = MagicMock(spec=Channel)
    ch.name = name
    ch.is_connected.return_value = True
    ch.owns_jid.side_effect = lambda j: j.startswith(jid_prefix)
    ch.formatter = TextFormatter()
    ch.send_event = AsyncMock()
    return ch


def _make_deps(channels):
    deps = MagicMock()
    type(deps).channels = PropertyMock(return_value=channels)
    return deps


# ---------------------------------------------------------------------------
# broadcast() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broadcast_sends_event_to_channels():
    ch = _make_channel("slack")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event)
    ch.send_event.assert_called_once_with("slack:C123", event)


@pytest.mark.asyncio
async def test_broadcast_skips_disconnected_channels():
    ch = _make_channel("slack")
    ch.is_connected.return_value = False
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event)
    ch.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_skips_channel_that_does_not_own_jid():
    ch = _make_channel("whatsapp", jid_prefix="wa:")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event)
    ch.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_broadcast_skip_channel_parameter():
    ch1 = _make_channel("slack")
    ch2 = _make_channel("slack2")
    ch2.owns_jid.side_effect = lambda j: j.startswith("slack:")
    deps = _make_deps([ch1, ch2])
    event = OutboundEvent(type=OutboundEventType.HOST, content="hello")
    await broadcast(deps, "slack:C123", event, skip_channel="slack")
    ch1.send_event.assert_not_called()
    ch2.send_event.assert_called_once_with("slack:C123", event)


@pytest.mark.asyncio
async def test_broadcast_keeps_delivery_successful_when_ledger_mark_fails(monkeypatch):
    ch = _make_channel("slack")
    deps = _make_deps([ch])
    monkeypatch.setattr(sender.state, "record_outbound", AsyncMock(return_value=1))
    monkeypatch.setattr(
        sender.state,
        "mark_delivered",
        AsyncMock(side_effect=RuntimeError("ledger unavailable")),
    )

    delivered = await broadcast(
        deps,
        "slack:C123",
        OutboundEvent(type=OutboundEventType.HOST, content="hello"),
    )

    assert delivered is True
    ch.send_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_broadcast_keeps_delivery_failure_best_effort_when_ledger_mark_fails(monkeypatch):
    ch = _make_channel("slack")
    ch.send_event.side_effect = OSError("network down")
    deps = _make_deps([ch])
    monkeypatch.setattr(sender.state, "record_outbound", AsyncMock(return_value=1))
    monkeypatch.setattr(
        sender.state,
        "mark_delivery_error",
        AsyncMock(side_effect=RuntimeError("ledger unavailable")),
    )

    delivered = await broadcast(
        deps,
        "slack:C123",
        OutboundEvent(type=OutboundEventType.HOST, content="hello"),
    )

    assert delivered is False
    ch.send_event.assert_awaited_once()


# ---------------------------------------------------------------------------
# finalize_stream_or_broadcast() tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_no_stream_falls_back_to_broadcast():
    ch = _make_channel("slack")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="done")
    await finalize_stream_or_broadcast(deps, "slack:C123", event, None)
    ch.send_event.assert_called_once_with("slack:C123", event)


@pytest.mark.asyncio
async def test_finalize_with_stream_updates_event():
    ch = _make_channel("slack")
    ch.update_event = AsyncMock()
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="final result")
    await finalize_stream_or_broadcast(deps, "slack:C123", event, {"slack": "msg-123"})
    ch.update_event.assert_called_once_with("slack:C123", "msg-123", event)
    ch.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_keeps_delivery_successful_when_ledger_write_fails(monkeypatch):
    ch = _make_channel("slack")
    ch.update_event = AsyncMock()
    deps = _make_deps([ch])
    monkeypatch.setattr(
        sender.state,
        "record_outbound_deliveries",
        AsyncMock(side_effect=RuntimeError("ledger unavailable")),
    )
    event = OutboundEvent(type=OutboundEventType.RESULT, content="final result")

    delivered = await finalize_stream_or_broadcast(deps, "slack:C123", event, {"slack": "msg-123"})

    assert delivered is True
    ch.update_event.assert_awaited_once_with("slack:C123", "msg-123", event)


@pytest.mark.asyncio
async def test_finalize_stream_update_failure_falls_back_to_send():
    ch = _make_channel("slack")
    ch.update_event = AsyncMock(side_effect=Exception("update failed"))
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="final result")
    await finalize_stream_or_broadcast(deps, "slack:C123", event, {"slack": "msg-123"})
    # Should fall back to send_event after update_event fails
    ch.send_event.assert_called_once_with("slack:C123", event)


@pytest.mark.asyncio
async def test_finalize_ignores_stream_id_for_channel_that_does_not_own_jid():
    ch = _make_channel("slack", jid_prefix="discord:")
    ch.update_event = AsyncMock()
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="final result")

    assert (
        await finalize_stream_or_broadcast(deps, "slack:C123", event, {"slack": "msg-123"}) is False
    )
    ch.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_suppresses_failed_stream_fallback_send():
    ch = _make_channel("slack")
    ch.update_event = AsyncMock(side_effect=Exception("update failed"))
    ch.send_event.side_effect = OSError("network down")
    deps = _make_deps([ch])
    event = OutboundEvent(type=OutboundEventType.RESULT, content="final result")

    assert (
        await finalize_stream_or_broadcast(deps, "slack:C123", event, {"slack": "msg-123"}) is False
    )
    ch.update_event.assert_awaited_once()
    ch.send_event.assert_awaited_once()
