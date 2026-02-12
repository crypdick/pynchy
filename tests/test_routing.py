"""Tests for routing and group availability.

Port of src/routing.test.ts — JID patterns, getAvailableGroups filtering/ordering.
"""

from __future__ import annotations

import pytest

from pynchy.app import PynchyApp
from pynchy.db import _init_test_database, store_chat_metadata
from pynchy.types import RegisteredGroup


@pytest.fixture
async def app():
    """Create a PynchyApp with a fresh in-memory database."""
    await _init_test_database()
    return PynchyApp()


# --- JID ownership patterns ---


class TestJidOwnership:
    def test_whatsapp_group_jid_ends_with_g_us(self):
        jid = "12345678@g.us"
        assert jid.endswith("@g.us")

    def test_whatsapp_dm_jid_ends_with_s_whatsapp_net(self):
        jid = "12345678@s.whatsapp.net"
        assert jid.endswith("@s.whatsapp.net")

    def test_unknown_jid_format_does_not_match(self):
        jid = "unknown:12345"
        assert not jid.endswith("@g.us")
        assert not jid.endswith("@s.whatsapp.net")


# --- get_available_groups ---


class TestGetAvailableGroups:
    async def test_returns_only_g_us_jids(self, app: PynchyApp):
        await store_chat_metadata("group1@g.us", "2024-01-01T00:00:01.000Z", "Group 1")
        await store_chat_metadata(
            "user@s.whatsapp.net", "2024-01-01T00:00:02.000Z", "User DM"
        )
        await store_chat_metadata("group2@g.us", "2024-01-01T00:00:03.000Z", "Group 2")

        groups = await app.get_available_groups()
        assert len(groups) == 2
        assert all(g["jid"].endswith("@g.us") for g in groups)

    async def test_excludes_group_sync_sentinel(self, app: PynchyApp):
        await store_chat_metadata("__group_sync__", "2024-01-01T00:00:00.000Z")
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:01.000Z", "Group")

        groups = await app.get_available_groups()
        assert len(groups) == 1
        assert groups[0]["jid"] == "group@g.us"

    async def test_marks_registered_groups_correctly(self, app: PynchyApp):
        await store_chat_metadata("reg@g.us", "2024-01-01T00:00:01.000Z", "Registered")
        await store_chat_metadata("unreg@g.us", "2024-01-01T00:00:02.000Z", "Unregistered")

        app.registered_groups = {
            "reg@g.us": RegisteredGroup(
                name="Registered",
                folder="registered",
                trigger="@pynchy",
                added_at="2024-01-01T00:00:00.000Z",
            ),
        }

        groups = await app.get_available_groups()
        reg = next(g for g in groups if g["jid"] == "reg@g.us")
        unreg = next(g for g in groups if g["jid"] == "unreg@g.us")

        assert reg["isRegistered"] is True
        assert unreg["isRegistered"] is False

    async def test_returns_groups_ordered_by_most_recent_activity(self, app: PynchyApp):
        await store_chat_metadata("old@g.us", "2024-01-01T00:00:01.000Z", "Old")
        await store_chat_metadata("new@g.us", "2024-01-01T00:00:05.000Z", "New")
        await store_chat_metadata("mid@g.us", "2024-01-01T00:00:03.000Z", "Mid")

        groups = await app.get_available_groups()
        assert groups[0]["jid"] == "new@g.us"
        assert groups[1]["jid"] == "mid@g.us"
        assert groups[2]["jid"] == "old@g.us"

    async def test_returns_empty_when_no_chats(self, app: PynchyApp):
        groups = await app.get_available_groups()
        assert len(groups) == 0
