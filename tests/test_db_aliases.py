"""Tests for the JID alias system — DB CRUD, inbound normalization, outbound translation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.db import (
    _init_test_database,
    get_aliases_for_jid,
    get_all_aliases,
    get_canonical_jid,
    set_jid_alias,
)


@pytest.fixture()
async def _db():
    await _init_test_database()


# ---------------------------------------------------------------------------
# DB CRUD
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestJidAliasCRUD:
    @pytest.mark.asyncio
    async def test_set_and_get_canonical(self):
        await set_jid_alias("slack:C123", "whatsapp@g.us", "slack")
        assert await get_canonical_jid("slack:C123") == "whatsapp@g.us"

    @pytest.mark.asyncio
    async def test_get_canonical_returns_none_for_unknown(self):
        assert await get_canonical_jid("unknown@jid") is None

    @pytest.mark.asyncio
    async def test_get_aliases_for_jid(self):
        await set_jid_alias("slack:C123", "wa@g.us", "slack")
        await set_jid_alias("tg:456", "wa@g.us", "telegram")

        aliases = await get_aliases_for_jid("wa@g.us")
        assert aliases == {"slack": "slack:C123", "telegram": "tg:456"}

    @pytest.mark.asyncio
    async def test_get_aliases_for_jid_returns_empty_for_unknown(self):
        assert await get_aliases_for_jid("unknown@g.us") == {}

    @pytest.mark.asyncio
    async def test_get_all_aliases(self):
        await set_jid_alias("slack:C1", "wa1@g.us", "slack")
        await set_jid_alias("slack:C2", "wa2@g.us", "slack")

        all_aliases = await get_all_aliases()
        assert all_aliases == {"slack:C1": "wa1@g.us", "slack:C2": "wa2@g.us"}

    @pytest.mark.asyncio
    async def test_upsert_overwrites_existing_alias(self):
        await set_jid_alias("slack:C1", "wa1@g.us", "slack")
        await set_jid_alias("slack:C1", "wa2@g.us", "slack")

        assert await get_canonical_jid("slack:C1") == "wa2@g.us"


# ---------------------------------------------------------------------------
# Inbound normalization (session_handler.on_inbound)
# ---------------------------------------------------------------------------


class TestInboundNormalization:
    @pytest.mark.asyncio
    async def test_alias_jid_resolved_to_canonical(self):
        """When a message arrives with an alias JID, it should be rewritten to canonical."""
        from pynchy.session_handler import on_inbound
        from pynchy.types import NewMessage

        msg = NewMessage(
            id="m1",
            chat_jid="slack:C123",
            sender="user1",
            sender_name="User",
            content="hello",
            timestamp="2024-01-01T00:00:00",
        )

        deps = MagicMock()
        deps.channels = []
        deps.workspaces = {}
        deps.resolve_canonical_jid = MagicMock(return_value="wa@g.us")
        deps.get_channel_jid = MagicMock(return_value=None)
        deps.emit = MagicMock()

        import pynchy.session_handler as sh

        original_ingest = sh.ingest_user_message
        captured_msgs: list[NewMessage] = []

        async def capture_ingest(deps, msg, *, source_channel=None):
            captured_msgs.append(msg)

        sh.ingest_user_message = capture_ingest
        try:
            await on_inbound(deps, "slack:C123", msg)
        finally:
            sh.ingest_user_message = original_ingest

        assert len(captured_msgs) == 1
        assert captured_msgs[0].chat_jid == "wa@g.us"

    @pytest.mark.asyncio
    async def test_non_alias_jid_passes_through(self):
        """When a message arrives with a canonical JID, it's not rewritten."""
        from pynchy.session_handler import on_inbound
        from pynchy.types import NewMessage

        msg = NewMessage(
            id="m2",
            chat_jid="wa@g.us",
            sender="user1",
            sender_name="User",
            content="hello",
            timestamp="2024-01-01T00:00:00",
        )

        deps = MagicMock()
        deps.channels = []
        deps.workspaces = {}
        deps.resolve_canonical_jid = MagicMock(return_value="wa@g.us")
        deps.get_channel_jid = MagicMock(return_value=None)
        deps.emit = MagicMock()

        import pynchy.session_handler as sh

        original_ingest = sh.ingest_user_message
        captured_msgs: list[NewMessage] = []

        async def capture_ingest(deps, msg, *, source_channel=None):
            captured_msgs.append(msg)

        sh.ingest_user_message = capture_ingest
        try:
            await on_inbound(deps, "wa@g.us", msg)
        finally:
            sh.ingest_user_message = original_ingest

        assert len(captured_msgs) == 1
        assert captured_msgs[0].chat_jid == "wa@g.us"


# ---------------------------------------------------------------------------
# Outbound translation (channel_handler.broadcast_to_channels)
# ---------------------------------------------------------------------------


class TestOutboundTranslation:
    @pytest.mark.asyncio
    async def test_uses_alias_jid_for_channel(self):
        """broadcast_to_channels should use the alias JID for channels that have one."""
        from pynchy.chat.bus import broadcast as broadcast_to_channels

        ch = MagicMock()
        ch.name = "slack"
        ch.is_connected.return_value = True
        ch.send_message = AsyncMock()

        deps = MagicMock()
        deps.channels = [ch]
        deps.get_channel_jid = MagicMock(return_value="slack:C123")

        await broadcast_to_channels(deps, "wa@g.us", "hello")

        ch.send_message.assert_awaited_once_with("slack:C123", "hello")

    @pytest.mark.asyncio
    async def test_falls_back_to_canonical_when_no_alias(self):
        """When no alias exists, use the canonical JID."""
        from pynchy.chat.bus import broadcast as broadcast_to_channels

        ch = MagicMock()
        ch.name = "whatsapp"
        ch.is_connected.return_value = True
        ch.send_message = AsyncMock()

        deps = MagicMock()
        deps.channels = [ch]
        deps.get_channel_jid = MagicMock(return_value=None)

        await broadcast_to_channels(deps, "wa@g.us", "hello")

        ch.send_message.assert_awaited_once_with("wa@g.us", "hello")

    @pytest.mark.asyncio
    async def test_each_channel_gets_its_own_jid(self):
        """Two channels should each receive the correct JID."""
        from pynchy.chat.bus import broadcast as broadcast_to_channels

        wa_ch = MagicMock()
        wa_ch.name = "whatsapp"
        wa_ch.is_connected.return_value = True
        wa_ch.send_message = AsyncMock()

        slack_ch = MagicMock()
        slack_ch.name = "slack"
        slack_ch.is_connected.return_value = True
        slack_ch.send_message = AsyncMock()

        def mock_get_channel_jid(canonical, channel_name):
            if channel_name == "slack":
                return "slack:C123"
            return None

        deps = MagicMock()
        deps.channels = [wa_ch, slack_ch]
        deps.get_channel_jid = MagicMock(side_effect=mock_get_channel_jid)

        await broadcast_to_channels(deps, "wa@g.us", "hello")

        wa_ch.send_message.assert_awaited_once_with("wa@g.us", "hello")
        slack_ch.send_message.assert_awaited_once_with("slack:C123", "hello")
