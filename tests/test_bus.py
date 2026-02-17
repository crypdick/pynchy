"""Tests for pynchy.messaging.bus — unified message broadcast."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.messaging.bus import broadcast, finalize_stream_or_broadcast

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(
    *,
    name: str = "test-ch",
    connected: bool = True,
    has_update: bool = False,
) -> MagicMock:
    ch = MagicMock()
    ch.name = name
    ch.is_connected.return_value = connected
    ch.send_message = AsyncMock()

    if has_update:
        ch.update_message = AsyncMock()
    else:
        del ch.update_message

    return ch


def _make_deps(channels: list | None = None) -> MagicMock:
    deps = MagicMock()
    deps.channels = channels or []
    deps.get_channel_jid = MagicMock(return_value=None)
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

        await broadcast(deps, "group@g.us", "hello")

        ch1.send_message.assert_awaited_once_with("group@g.us", "hello")
        ch2.send_message.assert_awaited_once_with("group@g.us", "hello")

    @pytest.mark.asyncio
    async def test_skips_disconnected_channels(self):
        ch = _make_channel(connected=False)
        deps = _make_deps([ch])

        await broadcast(deps, "group@g.us", "hello")

        ch.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_source_channel(self):
        ch1 = _make_channel(name="whatsapp")
        ch2 = _make_channel(name="telegram")
        deps = _make_deps([ch1, ch2])

        await broadcast(deps, "group@g.us", "hello", skip_channel="whatsapp")

        ch1.send_message.assert_not_awaited()
        ch2.send_message.assert_awaited_once_with("group@g.us", "hello")

    @pytest.mark.asyncio
    async def test_uses_jid_alias_when_available(self):
        ch = _make_channel(name="slack")
        deps = _make_deps([ch])
        deps.get_channel_jid.return_value = "slack-alias-jid"

        await broadcast(deps, "group@g.us", "hello")

        ch.send_message.assert_awaited_once_with("slack-alias-jid", "hello")
        deps.get_channel_jid.assert_called_with("group@g.us", "slack")

    @pytest.mark.asyncio
    async def test_falls_back_to_canonical_jid_when_no_alias(self):
        ch = _make_channel(name="wa")
        deps = _make_deps([ch])
        deps.get_channel_jid.return_value = None

        await broadcast(deps, "group@g.us", "hello")

        ch.send_message.assert_awaited_once_with("group@g.us", "hello")

    @pytest.mark.asyncio
    async def test_suppress_errors_catches_network_errors(self):
        ch = _make_channel()
        ch.send_message.side_effect = OSError("network down")
        deps = _make_deps([ch])

        # Should NOT raise
        await broadcast(deps, "group@g.us", "hello", suppress_errors=True)

    @pytest.mark.asyncio
    async def test_suppress_errors_does_not_catch_unexpected_errors(self):
        ch = _make_channel()
        ch.send_message.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])

        # RuntimeError is NOT in (OSError, TimeoutError, ConnectionError)
        with pytest.raises(RuntimeError, match="unexpected"):
            await broadcast(deps, "group@g.us", "hello", suppress_errors=True)

    @pytest.mark.asyncio
    async def test_no_suppress_catches_all_exceptions(self):
        ch = _make_channel()
        ch.send_message.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])

        # suppress_errors=False catches Exception (log but don't raise)
        await broadcast(deps, "group@g.us", "hello", suppress_errors=False)

    @pytest.mark.asyncio
    async def test_skip_channel_none_sends_to_all(self):
        ch1 = _make_channel(name="ch1")
        ch2 = _make_channel(name="ch2")
        deps = _make_deps([ch1, ch2])

        await broadcast(deps, "group@g.us", "hello", skip_channel=None)

        ch1.send_message.assert_awaited_once()
        ch2.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_channels_list(self):
        deps = _make_deps([])

        # Should not raise
        await broadcast(deps, "group@g.us", "hello")


# ---------------------------------------------------------------------------
# finalize_stream_or_broadcast()
# ---------------------------------------------------------------------------


class TestFinalizeStreamOrBroadcast:
    @pytest.mark.asyncio
    async def test_no_stream_ids_falls_back_to_broadcast(self):
        ch1 = _make_channel(name="ch1")
        ch2 = _make_channel(name="ch2")
        deps = _make_deps([ch1, ch2])

        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", None)

        ch1.send_message.assert_awaited_once_with("group@g.us", "final text")
        ch2.send_message.assert_awaited_once_with("group@g.us", "final text")

    @pytest.mark.asyncio
    async def test_empty_stream_ids_falls_back_to_broadcast(self):
        ch = _make_channel()
        deps = _make_deps([ch])

        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", {})

        ch.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_streamed_channel_in_place(self):
        ch = _make_channel(name="slack", has_update=True)
        deps = _make_deps([ch])

        stream_ids = {"slack": "msg-123"}
        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", stream_ids)

        ch.update_message.assert_awaited_once_with("group@g.us", "msg-123", "final text")
        ch.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_streaming_channel_gets_normal_send(self):
        """Channel without a stream message_id should get send_message."""
        ch_streaming = _make_channel(name="slack", has_update=True)
        ch_normal = _make_channel(name="whatsapp")
        deps = _make_deps([ch_streaming, ch_normal])

        stream_ids = {"slack": "msg-123"}  # Only slack was streaming
        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", stream_ids)

        ch_streaming.update_message.assert_awaited_once()
        ch_normal.send_message.assert_awaited_once_with("group@g.us", "final text")

    @pytest.mark.asyncio
    async def test_uses_jid_alias_for_non_streaming_fallback(self):
        """Non-streaming fallback within finalize should resolve JID aliases."""
        ch = _make_channel(name="slack")
        deps = _make_deps([ch])
        deps.get_channel_jid.return_value = "slack-alias"

        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", {"other": "x"})

        ch.send_message.assert_awaited_once_with("slack-alias", "final text")

    @pytest.mark.asyncio
    async def test_skips_disconnected_channels_in_fallback(self):
        ch = _make_channel(name="wa", connected=False)
        deps = _make_deps([ch])

        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", {"other": "x"})

        ch.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_update_failure_is_suppressed(self):
        ch = _make_channel(name="slack", has_update=True)
        ch.update_message.side_effect = OSError("network")
        deps = _make_deps([ch])

        # Should not raise
        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", {"slack": "msg-1"})

    @pytest.mark.asyncio
    async def test_fallback_send_failure_is_suppressed(self):
        ch = _make_channel(name="wa")
        ch.send_message.side_effect = OSError("network")
        deps = _make_deps([ch])

        # Should not raise — errors are caught in the finalize path
        await finalize_stream_or_broadcast(deps, "group@g.us", "final text", {"other": "x"})
