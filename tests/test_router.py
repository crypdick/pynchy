"""Tests for router.py functions - route_outbound and find_channel.

These tests focus on channel routing logic that is not covered elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pynchy.router import find_channel, route_outbound


# --- Test Helpers ---


@dataclass
class FakeChannel:
    """Minimal channel stub for testing routing."""

    name: str
    jids: list[str]
    connected: bool = True
    messages_sent: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.messages_sent is None:
            self.messages_sent = []

    def owns_jid(self, jid: str) -> bool:
        return jid in self.jids

    def is_connected(self) -> bool:
        return self.connected

    async def send_message(self, jid: str, text: str) -> None:
        self.messages_sent.append((jid, text))


# --- find_channel ---


class TestFindChannel:
    """Test the find_channel function which locates the channel owning a JID."""

    def test_finds_channel_by_jid(self):
        ch1 = FakeChannel("channel1", ["jid1@g.us"])
        ch2 = FakeChannel("channel2", ["jid2@g.us"])
        channels = [ch1, ch2]

        result = find_channel(channels, "jid2@g.us")
        assert result is ch2

    def test_returns_none_when_no_match(self):
        ch1 = FakeChannel("channel1", ["jid1@g.us"])
        channels = [ch1]

        result = find_channel(channels, "unknown@g.us")
        assert result is None

    def test_returns_first_matching_channel(self):
        # In case multiple channels claim the same JID, return the first
        ch1 = FakeChannel("channel1", ["shared@g.us"])
        ch2 = FakeChannel("channel2", ["shared@g.us"])
        channels = [ch1, ch2]

        result = find_channel(channels, "shared@g.us")
        assert result is ch1

    def test_returns_none_for_empty_channel_list(self):
        result = find_channel([], "jid@g.us")
        assert result is None

    def test_channel_can_own_multiple_jids(self):
        ch = FakeChannel("channel", ["jid1@g.us", "jid2@g.us", "jid3@g.us"])
        channels = [ch]

        assert find_channel(channels, "jid1@g.us") is ch
        assert find_channel(channels, "jid2@g.us") is ch
        assert find_channel(channels, "jid3@g.us") is ch


# --- route_outbound ---


class TestRouteOutbound:
    """Test the route_outbound function which sends messages to the appropriate channel."""

    async def test_routes_to_connected_channel(self):
        ch = FakeChannel("channel", ["jid@g.us"], connected=True)
        channels = [ch]

        await route_outbound(channels, "jid@g.us", "Hello world")

        assert len(ch.messages_sent) == 1
        assert ch.messages_sent[0] == ("jid@g.us", "Hello world")

    async def test_raises_when_no_channel_owns_jid(self):
        ch = FakeChannel("channel", ["other@g.us"], connected=True)
        channels = [ch]

        with pytest.raises(RuntimeError, match="No channel for JID: unknown@g.us"):
            await route_outbound(channels, "unknown@g.us", "test")

    async def test_raises_when_channel_not_connected(self):
        ch = FakeChannel("channel", ["jid@g.us"], connected=False)
        channels = [ch]

        with pytest.raises(RuntimeError, match="No channel for JID: jid@g.us"):
            await route_outbound(channels, "jid@g.us", "test")

    async def test_prefers_connected_channel_over_disconnected(self):
        # If multiple channels own a JID, use the connected one
        ch1 = FakeChannel("disconnected", ["jid@g.us"], connected=False)
        ch2 = FakeChannel("connected", ["jid@g.us"], connected=True)
        channels = [ch1, ch2]

        await route_outbound(channels, "jid@g.us", "test message")

        assert len(ch1.messages_sent) == 0
        assert len(ch2.messages_sent) == 1
        assert ch2.messages_sent[0] == ("jid@g.us", "test message")

    async def test_routes_to_first_connected_channel_when_multiple_match(self):
        # If multiple connected channels own a JID, use the first
        ch1 = FakeChannel("first", ["jid@g.us"], connected=True)
        ch2 = FakeChannel("second", ["jid@g.us"], connected=True)
        channels = [ch1, ch2]

        await route_outbound(channels, "jid@g.us", "test")

        assert len(ch1.messages_sent) == 1
        assert len(ch2.messages_sent) == 0

    async def test_routes_empty_message(self):
        ch = FakeChannel("channel", ["jid@g.us"], connected=True)
        channels = [ch]

        await route_outbound(channels, "jid@g.us", "")

        assert ch.messages_sent[0] == ("jid@g.us", "")

    async def test_routes_multiline_message(self):
        ch = FakeChannel("channel", ["jid@g.us"], connected=True)
        channels = [ch]

        message = "Line 1\nLine 2\nLine 3"
        await route_outbound(channels, "jid@g.us", message)

        assert ch.messages_sent[0] == ("jid@g.us", message)

    async def test_raises_when_empty_channel_list(self):
        with pytest.raises(RuntimeError, match="No channel for JID"):
            await route_outbound([], "jid@g.us", "test")
