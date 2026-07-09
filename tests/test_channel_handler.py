"""Tests for pynchy.channel_handler — channel broadcasting, reactions, and typing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.host.orchestrator.messaging.channel_handler import (
    processing_ack_emoji,
    send_reaction_to_channels,
    send_reaction_to_outbound,
    set_typing_on_channels,
)
from pynchy.host.orchestrator.messaging.sender import BusDeps
from pynchy.host.orchestrator.messaging.sender import broadcast as broadcast_to_channels
from pynchy.types import Channel, OutboundEvent, OutboundEventType

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
    ch = MagicMock(spec=Channel)
    ch.name = name
    ch.is_connected.return_value = connected
    ch.send_event = AsyncMock()

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
    deps = MagicMock(spec=BusDeps)
    deps.channels = channels or []
    deps.workspaces = {}
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
        event = OutboundEvent(type=OutboundEventType.HOST, content="hello")

        await broadcast_to_channels(deps, "group@g.us", event)

        ch1.send_event.assert_awaited_once_with("group@g.us", event)
        ch2.send_event.assert_awaited_once_with("group@g.us", event)

    @pytest.mark.asyncio
    async def test_skips_disconnected_channels(self):
        ch = _make_channel(connected=False)
        deps = _make_deps([ch])
        event = OutboundEvent(type=OutboundEventType.HOST, content="hello")

        await broadcast_to_channels(deps, "group@g.us", event)

        ch.send_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suppress_errors_catches_network_errors(self):
        ch = _make_channel()
        ch.send_event.side_effect = OSError("network down")
        deps = _make_deps([ch])
        event = OutboundEvent(type=OutboundEventType.HOST, content="hello")

        # Should NOT raise
        await broadcast_to_channels(deps, "group@g.us", event, suppress_errors=True)

    @pytest.mark.asyncio
    async def test_suppress_errors_does_not_catch_unexpected_errors(self):
        ch = _make_channel()
        ch.send_event.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])
        event = OutboundEvent(type=OutboundEventType.HOST, content="hello")

        # RuntimeError is not in (OSError, TimeoutError, ConnectionError)
        with pytest.raises(RuntimeError, match="unexpected"):
            await broadcast_to_channels(deps, "group@g.us", event, suppress_errors=True)

    @pytest.mark.asyncio
    async def test_no_suppress_catches_all_exceptions(self):
        ch = _make_channel()
        ch.send_event.side_effect = RuntimeError("unexpected")
        deps = _make_deps([ch])
        event = OutboundEvent(type=OutboundEventType.HOST, content="hello")

        # suppress_errors=False catches Exception
        await broadcast_to_channels(deps, "group@g.us", event, suppress_errors=False)


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
# processing_ack_emoji
# ---------------------------------------------------------------------------


class TestProcessingAckEmoji:
    def test_uses_channel_specific_processing_ack(self):
        ch = _make_channel(name="discord", connected=True)
        ch.owns_jid.return_value = True
        ch.processing_ack_emoji = MagicMock(return_value="👀")
        deps = _make_deps([ch])

        assert processing_ack_emoji(deps, "group@g.us") == "👀"

    def test_none_disables_processing_ack(self):
        ch = _make_channel(name="discord", connected=True)
        ch.owns_jid.return_value = True
        ch.processing_ack_emoji = MagicMock(return_value=None)
        deps = _make_deps([ch])

        assert processing_ack_emoji(deps, "group@g.us") is None

    def test_default_used_when_channel_has_no_preference(self):
        ch = _make_channel(name="slack", connected=True)
        ch.owns_jid.return_value = True
        deps = _make_deps([ch])

        assert processing_ack_emoji(deps, "group@g.us") == "🦞"


# ---------------------------------------------------------------------------
# set_typing_on_channels
# ---------------------------------------------------------------------------


class TestSetTypingOnChannels:
    @pytest.mark.asyncio
    async def test_requires_keyword_bool(self):
        ch = _make_channel(has_typing=True)
        deps = _make_deps([ch])

        with pytest.raises(TypeError):
            await set_typing_on_channels(deps, "group@g.us", True)

    @pytest.mark.asyncio
    async def test_sends_typing_to_capable_channels(self):
        ch = _make_channel(has_typing=True)
        deps = _make_deps([ch])

        await set_typing_on_channels(deps, "group@g.us", is_typing=True)

        ch.set_typing.assert_awaited_once_with("group@g.us", is_typing=True)

    @pytest.mark.asyncio
    async def test_skips_channels_without_set_typing(self):
        ch = _make_channel(has_typing=False)
        deps = _make_deps([ch])

        await set_typing_on_channels(deps, "group@g.us", is_typing=True)

    @pytest.mark.asyncio
    async def test_catches_network_errors(self):
        ch = _make_channel(has_typing=True)
        ch.set_typing.side_effect = TimeoutError("timeout")
        deps = _make_deps([ch])

        await set_typing_on_channels(deps, "group@g.us", is_typing=True)


# ---------------------------------------------------------------------------
# send_reaction_to_outbound
# ---------------------------------------------------------------------------


class TestSendReactionToOutbound:
    @pytest.mark.asyncio
    async def test_sends_reaction_with_per_channel_ids(self):
        ch = _make_channel(name="slack", connected=True, has_reaction=True)
        deps = _make_deps([ch])
        per_channel_ids = {"slack": "1234567890.000001"}

        await send_reaction_to_outbound(deps, "group@g.us", per_channel_ids, "zzz")

        ch.send_reaction.assert_awaited_once_with(
            "group@g.us", "slack-1234567890.000001", "", "zzz"
        )

    @pytest.mark.asyncio
    async def test_skips_channels_without_ids(self):
        ch = _make_channel(name="slack", connected=True, has_reaction=True)
        deps = _make_deps([ch])
        per_channel_ids = {"other-channel": "1234567890.000001"}

        await send_reaction_to_outbound(deps, "group@g.us", per_channel_ids, "zzz")

        ch.send_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_channels_without_send_reaction(self):
        ch = _make_channel(name="tui", connected=True, has_reaction=False)
        deps = _make_deps([ch])
        per_channel_ids = {"tui": "some-id"}

        await send_reaction_to_outbound(deps, "group@g.us", per_channel_ids, "zzz")
        # No error, no call
