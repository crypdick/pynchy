"""Tests for pynchy.channel_handler — channel broadcasting, reactions, typing, and host messages."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.messaging.channel_handler import (
    broadcast_host_message,
    broadcast_to_channels,
    send_reaction_to_channels,
    set_typing_on_channels,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_channel(
    *,
    name: str = "test-ch",
    connected: bool = True,
    has_reaction: bool = False,
    has_typing: bool = False,
) -> MagicMock:
    ch = MagicMock()
    ch.name = name
    ch.is_connected.return_value = connected
    ch.send_message = AsyncMock()

    if has_reaction:
        ch.send_reaction = AsyncMock()
    else:
        del ch.send_reaction  # ensure hasattr returns False

    if has_typing:
        ch.set_typing = AsyncMock()
    else:
        del ch.set_typing

    return ch


def _make_deps(channels: list | None = None) -> MagicMock:
    deps = MagicMock()
    deps.channels = channels or []
    deps.event_bus = MagicMock()
    deps.get_channel_jid = MagicMock(return_value=None)
    return deps


# ---------------------------------------------------------------------------
# broadcast_to_channels
# ---------------------------------------------------------------------------


class TestBroadcastToChannels:
    @pytest.mark.asyncio
    async def test_sends_to_connected_channels(self):
        ch1 = _make_channel(name="ch1")
        ch2 = _make_channel(name="ch2")
        deps = _make_deps([ch1, ch2])

        await broadcast_to_channels(deps, "group@g.us", "hello")

        ch1.send_message.assert_awaited_once_with("group@g.us", "hello")
        ch2.send_message.assert_awaited_once_with("group@g.us", "hello")

    @pytest.mark.asyncio
    async def test_skips_disconnected_channels(self):
        ch = _make_channel(connected=False)
        deps = _make_deps([ch])

        await broadcast_to_channels(deps, "group@g.us", "hello")

        ch.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suppress_errors_catches_network_errors(self):
        ch = _make_channel()
        ch.send_message.side_effect = OSError("network down")
        deps = _make_deps([ch])

        # Should NOT raise
        await broadcast_to_channels(deps, "group@g.us", "hello", suppress_errors=True)

    @pytest.mark.asyncio
    async def test_suppress_errors_does_not_catch_unexpected_errors(self):
        ch = _make_channel()
        ch.send_message.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])

        # RuntimeError is not in (OSError, TimeoutError, ConnectionError)
        with pytest.raises(RuntimeError, match="unexpected"):
            await broadcast_to_channels(deps, "group@g.us", "hello", suppress_errors=True)

    @pytest.mark.asyncio
    async def test_no_suppress_catches_all_exceptions(self):
        ch = _make_channel()
        ch.send_message.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])

        # suppress_errors=False catches Exception
        await broadcast_to_channels(deps, "group@g.us", "hello", suppress_errors=False)


# ---------------------------------------------------------------------------
# send_reaction_to_channels
# ---------------------------------------------------------------------------


class TestSendReactionToChannels:
    @pytest.mark.asyncio
    async def test_sends_to_channels_with_send_reaction(self):
        ch = _make_channel(has_reaction=True)
        deps = _make_deps([ch])

        await send_reaction_to_channels(deps, "group@g.us", "msg-1", "user@s", "👀")

        ch.send_reaction.assert_awaited_once_with("group@g.us", "msg-1", "user@s", "👀")

    @pytest.mark.asyncio
    async def test_skips_channels_without_send_reaction(self):
        ch = _make_channel(has_reaction=False)
        deps = _make_deps([ch])

        await send_reaction_to_channels(deps, "group@g.us", "msg-1", "user@s", "👀")
        # No error; no call (no send_reaction attribute)

    @pytest.mark.asyncio
    async def test_skips_disconnected_channels(self):
        ch = _make_channel(connected=False, has_reaction=True)
        deps = _make_deps([ch])

        await send_reaction_to_channels(deps, "group@g.us", "msg-1", "user@s", "👀")

        ch.send_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_catches_network_errors(self):
        ch = _make_channel(has_reaction=True)
        ch.send_reaction.side_effect = OSError("network")
        deps = _make_deps([ch])

        await send_reaction_to_channels(deps, "group@g.us", "msg-1", "user@s", "👀")


# ---------------------------------------------------------------------------
# set_typing_on_channels
# ---------------------------------------------------------------------------


class TestSetTypingOnChannels:
    @pytest.mark.asyncio
    async def test_sends_typing_to_capable_channels(self):
        ch = _make_channel(has_typing=True)
        deps = _make_deps([ch])

        await set_typing_on_channels(deps, "group@g.us", True)

        ch.set_typing.assert_awaited_once_with("group@g.us", True)

    @pytest.mark.asyncio
    async def test_skips_channels_without_set_typing(self):
        ch = _make_channel(has_typing=False)
        deps = _make_deps([ch])

        await set_typing_on_channels(deps, "group@g.us", True)

    @pytest.mark.asyncio
    async def test_catches_network_errors(self):
        ch = _make_channel(has_typing=True)
        ch.set_typing.side_effect = TimeoutError("timeout")
        deps = _make_deps([ch])

        await set_typing_on_channels(deps, "group@g.us", True)


# ---------------------------------------------------------------------------
# broadcast_host_message
# ---------------------------------------------------------------------------


class TestBroadcastHostMessage:
    @pytest.mark.asyncio
    async def test_stores_and_broadcasts_host_message(self):
        ch = _make_channel()
        deps = _make_deps([ch])

        with patch(
            "pynchy.messaging.channel_handler.store_message_direct",
            new_callable=AsyncMock,
        ) as mock_store:
            await broadcast_host_message(deps, "group@g.us", "⚠️ Error occurred")

            # Verify DB storage
            mock_store.assert_awaited_once()
            call_kwargs = mock_store.call_args[1]
            assert call_kwargs["sender"] == "host"
            assert call_kwargs["content"] == "⚠️ Error occurred"
            assert call_kwargs["message_type"] == "host"

            # Verify channel broadcast includes house emoji prefix
            ch.send_message.assert_awaited_once()
            sent_text = ch.send_message.call_args[0][1]
            assert "🏠" in sent_text
            assert "Error occurred" in sent_text

            # Verify event bus emission
            deps.event_bus.emit.assert_called_once()
            event = deps.event_bus.emit.call_args[0][0]
            assert event.sender_name == "host"
            assert event.is_bot is True
