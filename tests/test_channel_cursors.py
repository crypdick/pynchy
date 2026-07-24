"""Tests for per-channel bidirectional cursor CRUD."""

from __future__ import annotations

import pytest
from conftest import init_test_database

from pynchy.state import (
    advance_cursors_atomic,
    get_channel_cursor,
    prune_stale_cursors,
    set_channel_cursor,
)


@pytest.fixture
async def _db():
    await init_test_database()


@pytest.mark.usefixtures("_db")
class TestGetChannelCursor:
    @pytest.mark.asyncio
    async def test_returns_empty_string_when_no_cursor(self):
        result = await get_channel_cursor("slack", "group@g.us", "inbound")
        assert not result

    @pytest.mark.asyncio
    async def test_returns_value_after_set(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
        result = await get_channel_cursor("slack", "group@g.us", "inbound")
        assert result == "2024-01-01T00:00:00"

    @pytest.mark.asyncio
    async def test_different_directions_are_independent(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("slack", "group@g.us", "outbound", "2024-06-01")

        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-01-01"
        assert await get_channel_cursor("slack", "group@g.us", "outbound") == "2024-06-01"

    @pytest.mark.asyncio
    async def test_different_channels_are_independent(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("whatsapp", "group@g.us", "inbound", "2024-02-01")

        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-01-01"
        assert await get_channel_cursor("whatsapp", "group@g.us", "inbound") == "2024-02-01"

    @pytest.mark.asyncio
    async def test_different_groups_are_independent(self):
        await set_channel_cursor("slack", "group1@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("slack", "group2@g.us", "inbound", "2024-03-01")

        assert await get_channel_cursor("slack", "group1@g.us", "inbound") == "2024-01-01"
        assert await get_channel_cursor("slack", "group2@g.us", "inbound") == "2024-03-01"


@pytest.mark.usefixtures("_db")
class TestSetChannelCursor:
    @pytest.mark.asyncio
    async def test_upsert_overwrites_existing(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-06-01")

        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-06-01"


@pytest.mark.usefixtures("_db")
class TestAdvanceCursorsAtomic:
    @pytest.mark.asyncio
    async def test_advances_both_directions(self):
        await advance_cursors_atomic(
            "slack", "group@g.us", inbound="2024-03-01", outbound="2024-03-02"
        )

        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-03-01"
        assert await get_channel_cursor("slack", "group@g.us", "outbound") == "2024-03-02"

    @pytest.mark.asyncio
    async def test_advances_inbound_only(self):
        await set_channel_cursor("slack", "group@g.us", "outbound", "2024-01-01")
        await advance_cursors_atomic("slack", "group@g.us", inbound="2024-03-01")

        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-03-01"
        # Outbound unchanged
        assert await get_channel_cursor("slack", "group@g.us", "outbound") == "2024-01-01"

    @pytest.mark.asyncio
    async def test_noop_when_both_none(self):
        """No cursors are written when both values are None."""
        await advance_cursors_atomic("slack", "group@g.us", inbound=None, outbound=None)

        assert not await get_channel_cursor("slack", "group@g.us", "inbound")
        assert not await get_channel_cursor("slack", "group@g.us", "outbound")

    @pytest.mark.asyncio
    async def test_never_regresses_cursor(self):
        """Advancing with an older value keeps the newer stored cursor."""
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-06-01")
        await advance_cursors_atomic("slack", "group@g.us", inbound="2024-01-01")

        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-06-01"

    @pytest.mark.asyncio
    async def test_advances_past_existing_cursor(self):
        """Advancing with a newer value updates the stored cursor."""
        await set_channel_cursor("slack", "group@g.us", "outbound", "2024-01-01")
        await advance_cursors_atomic("slack", "group@g.us", outbound="2024-06-01")

        assert await get_channel_cursor("slack", "group@g.us", "outbound") == "2024-06-01"


@pytest.mark.usefixtures("_db")
class TestPruneStaleCursors:
    @pytest.mark.asyncio
    async def test_deletes_cursors_outside_the_active_channel_workspace_pairs(self):
        await set_channel_cursor("old-channel", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("active-channel", "removed@g.us", "inbound", "2024-02-01")
        await set_channel_cursor("active-channel", "removed@g.us", "outbound", "2024-03-01")
        await set_channel_cursor("active-channel", "group@g.us", "inbound", "2024-06-01")

        pruned = await prune_stale_cursors({("active-channel", "group@g.us")})

        assert pruned == 3
        assert not await get_channel_cursor("old-channel", "group@g.us", "inbound")
        assert not await get_channel_cursor("active-channel", "removed@g.us", "inbound")
        assert not await get_channel_cursor("active-channel", "removed@g.us", "outbound")
        assert await get_channel_cursor("active-channel", "group@g.us", "inbound") == "2024-06-01"

    @pytest.mark.asyncio
    async def test_noop_when_all_channel_workspace_pairs_are_active(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01")

        pruned = await prune_stale_cursors({("slack", "group@g.us")})

        assert pruned == 0
        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-01-01"

    @pytest.mark.asyncio
    async def test_noop_on_empty_table(self):
        pruned = await prune_stale_cursors({("slack", "group@g.us")})
        assert pruned == 0

    @pytest.mark.asyncio
    async def test_empty_active_pair_set_preserves_recovery_cursors(self):
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("discord", "admin@g.us", "outbound", "2024-02-01")

        pruned = await prune_stale_cursors(set())

        assert pruned == 0
        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-01-01"
        assert await get_channel_cursor("discord", "admin@g.us", "outbound") == "2024-02-01"
