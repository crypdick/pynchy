"""Tests for pynchy.host.orchestrator.messaging.sender — unified message broadcast."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.host.orchestrator.messaging.sender import broadcast, finalize_stream_or_broadcast
from pynchy.types import OutboundEvent, OutboundEventType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(content: str = "hello") -> OutboundEvent:
    return OutboundEvent(type=OutboundEventType.HOST, content=content)


def _make_channel(
    *,
    name: str = "test-ch",
    connected: bool = True,
    has_update: bool = False,
) -> MagicMock:
    ch = MagicMock()
    ch.name = name
    ch.is_connected.return_value = connected
    ch.send_event = AsyncMock()

    if has_update:
        ch.update_event = AsyncMock()
    else:
        # Ensure hasattr(ch, "update_event") returns False
        del ch.update_event

    return ch


def _make_deps(channels: list | None = None) -> MagicMock:
    deps = MagicMock()
    deps.channels = channels or []
    deps.workspaces = {}
    return deps


# ---------------------------------------------------------------------------
# broadcast()
# ---------------------------------------------------------------------------


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_sends_to_all_connected_channels(self):
        ch1 = _make_channel(name="ch1")
        ch2 = _make_channel(name="ch2")
        deps = _make_deps([ch1, ch2])
        event = _make_event("hello")

        await broadcast(deps, "group@g.us", event)

        ch1.send_event.assert_awaited_once_with("group@g.us", event)
        ch2.send_event.assert_awaited_once_with("group@g.us", event)

    @pytest.mark.asyncio
    async def test_skips_disconnected_channels(self):
        ch = _make_channel(connected=False)
        deps = _make_deps([ch])

        await broadcast(deps, "group@g.us", _make_event())

        ch.send_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_source_channel(self):
        ch1 = _make_channel(name="whatsapp")
        ch2 = _make_channel(name="telegram")
        deps = _make_deps([ch1, ch2])
        event = _make_event("hello")

        await broadcast(deps, "group@g.us", event, skip_channel="whatsapp")

        ch1.send_event.assert_not_awaited()
        ch2.send_event.assert_awaited_once_with("group@g.us", event)

    @pytest.mark.asyncio
    async def test_suppress_errors_catches_network_errors(self):
        ch = _make_channel()
        ch.send_event.side_effect = OSError("network down")
        deps = _make_deps([ch])

        # Should NOT raise
        await broadcast(deps, "group@g.us", _make_event(), suppress_errors=True)

    @pytest.mark.asyncio
    async def test_suppress_errors_does_not_catch_unexpected_errors(self):
        ch = _make_channel()
        ch.send_event.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])

        # RuntimeError is NOT in (OSError, TimeoutError, ConnectionError)
        with pytest.raises(RuntimeError, match="unexpected"):
            await broadcast(deps, "group@g.us", _make_event(), suppress_errors=True)

    @pytest.mark.asyncio
    async def test_no_suppress_catches_all_exceptions(self):
        ch = _make_channel()
        ch.send_event.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])

        # suppress_errors=False catches Exception (log but don't raise)
        await broadcast(deps, "group@g.us", _make_event(), suppress_errors=False)

    @pytest.mark.asyncio
    async def test_skip_channel_none_sends_to_all(self):
        ch1 = _make_channel(name="ch1")
        ch2 = _make_channel(name="ch2")
        deps = _make_deps([ch1, ch2])

        await broadcast(deps, "group@g.us", _make_event(), skip_channel=None)

        ch1.send_event.assert_awaited_once()
        ch2.send_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_channel_that_doesnt_own_jid(self):
        """Channels that don't own the canonical JID should be skipped."""
        ch = _make_channel(name="slack")
        ch.owns_jid = MagicMock(return_value=False)
        deps = _make_deps([ch])

        await broadcast(deps, "group@g.us", _make_event())

        ch.send_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sends_when_channel_owns_jid(self):
        """Channel that owns the canonical JID should receive messages."""
        ch = _make_channel(name="whatsapp")
        ch.owns_jid = MagicMock(return_value=True)
        deps = _make_deps([ch])
        event = _make_event("hello")

        await broadcast(deps, "group@g.us", event)

        ch.send_event.assert_awaited_once_with("group@g.us", event)

    @pytest.mark.asyncio
    async def test_empty_channels_list(self):
        deps = _make_deps([])

        # Should not raise
        await broadcast(deps, "group@g.us", _make_event())


# ---------------------------------------------------------------------------
# finalize_stream_or_broadcast()
# ---------------------------------------------------------------------------


class TestFinalizeStreamOrBroadcast:
    @pytest.mark.asyncio
    async def test_no_stream_ids_falls_back_to_broadcast(self):
        ch1 = _make_channel(name="ch1")
        ch2 = _make_channel(name="ch2")
        deps = _make_deps([ch1, ch2])
        event = _make_event("final text")

        await finalize_stream_or_broadcast(deps, "group@g.us", event, None)

        ch1.send_event.assert_awaited_once_with("group@g.us", event)
        ch2.send_event.assert_awaited_once_with("group@g.us", event)

    @pytest.mark.asyncio
    async def test_empty_stream_ids_falls_back_to_broadcast(self):
        ch = _make_channel()
        deps = _make_deps([ch])

        await finalize_stream_or_broadcast(deps, "group@g.us", _make_event("final text"), {})

        ch.send_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_streamed_channel_in_place(self):
        ch = _make_channel(name="slack", has_update=True)
        deps = _make_deps([ch])
        event = _make_event("final text")

        stream_ids = {"slack": "msg-123"}
        await finalize_stream_or_broadcast(deps, "group@g.us", event, stream_ids)

        ch.update_event.assert_awaited_once_with("group@g.us", "msg-123", event)
        ch.send_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_streaming_channel_gets_normal_send(self):
        """Channel without a stream message_id should get send_event."""
        ch_streaming = _make_channel(name="slack", has_update=True)
        ch_normal = _make_channel(name="whatsapp")
        deps = _make_deps([ch_streaming, ch_normal])
        event = _make_event("final text")

        stream_ids = {"slack": "msg-123"}  # Only slack was streaming
        await finalize_stream_or_broadcast(deps, "group@g.us", event, stream_ids)

        ch_streaming.update_event.assert_awaited_once()
        ch_normal.send_event.assert_awaited_once_with("group@g.us", event)

    @pytest.mark.asyncio
    async def test_skips_disconnected_channels_in_fallback(self):
        ch = _make_channel(name="wa", connected=False)
        deps = _make_deps([ch])

        await finalize_stream_or_broadcast(
            deps, "group@g.us", _make_event("final text"), {"other": "x"}
        )

        ch.send_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_update_failure_is_suppressed(self):
        ch = _make_channel(name="slack", has_update=True)
        ch.update_event.side_effect = OSError("network")
        deps = _make_deps([ch])

        # Should not raise
        await finalize_stream_or_broadcast(
            deps, "group@g.us", _make_event("final text"), {"slack": "msg-1"}
        )

    @pytest.mark.asyncio
    async def test_fallback_send_failure_is_suppressed(self):
        ch = _make_channel(name="wa")
        ch.send_event.side_effect = OSError("network")
        ch.owns_jid = MagicMock(return_value=True)
        deps = _make_deps([ch])

        # Should not raise — errors are caught in the finalize path
        await finalize_stream_or_broadcast(
            deps, "group@g.us", _make_event("final text"), {"other": "x"}
        )

    @pytest.mark.asyncio
    async def test_finalize_skips_channel_without_ownership(self):
        """Non-streaming channel without JID ownership should be skipped."""
        ch = _make_channel(name="slack")
        ch.owns_jid = MagicMock(return_value=False)
        deps = _make_deps([ch])

        await finalize_stream_or_broadcast(deps, "group@g.us", _make_event("text"), {"other": "x"})

        ch.send_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suppress_errors_lets_unexpected_errors_propagate_non_streaming(self):
        """suppress_errors=True should let non-network errors propagate (matches broadcast)."""
        ch = _make_channel(name="wa")
        ch.send_event.side_effect = RuntimeError("bug in channel code")
        deps = _make_deps([ch])

        with pytest.raises(RuntimeError, match="bug in channel code"):
            await finalize_stream_or_broadcast(
                deps, "group@g.us", _make_event("text"), {"other": "x"}, suppress_errors=True
            )

    @pytest.mark.asyncio
    async def test_suppress_errors_lets_unexpected_errors_propagate_stream_fallback(self):
        """suppress_errors=True should let non-network errors propagate from stream fallback."""
        ch = _make_channel(name="slack", has_update=True)
        ch.update_event.side_effect = OSError("stream update failed")
        ch.send_event.side_effect = RuntimeError("bug in channel code")
        deps = _make_deps([ch])

        with pytest.raises(RuntimeError, match="bug in channel code"):
            await finalize_stream_or_broadcast(
                deps, "group@g.us", _make_event("text"), {"slack": "msg-1"}, suppress_errors=True
            )

    @pytest.mark.asyncio
    async def test_no_suppress_catches_all_in_finalize(self):
        """suppress_errors=False should catch all exceptions (matches broadcast)."""
        ch = _make_channel(name="wa")
        ch.send_event.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])

        # Should NOT raise — suppress_errors=False catches Exception
        await finalize_stream_or_broadcast(
            deps, "group@g.us", _make_event("text"), {"other": "x"}, suppress_errors=False
        )
