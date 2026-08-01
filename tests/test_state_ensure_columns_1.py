"""Tests for messaging statistics."""

from __future__ import annotations

import pytest

from pynchy.state import (
    get_messaging_stats,
    mark_delivered,
    record_outbound,
    store_chat_metadata,
)
from tests.state_support import _store, _store_message_row

pytest_plugins = ("tests.state_support",)


@pytest.mark.anyio
class TestMessagingStats:
    async def test_empty_db_returns_zeros(self):
        result = await get_messaging_stats()
        assert result["total_inbound"] == 0
        assert result["total_outbound"] == 0
        assert result["last_received_at"] is None
        assert result["last_sent_at"] is None
        assert result["pending_deliveries"] == 0

    async def test_counts_inbound_and_outbound(self):
        await store_chat_metadata("g@g.us", "2026-01-01T00:00:00", "Test")
        await _store_message_row(
            _store(
                message_id="m1",
                chat_jid="g@g.us",
                sender="u@s",
                sender_name="Alice",
                content="hello",
                timestamp="2026-02-20T10:00:00",
            )
        )
        await _store_message_row(
            _store(
                message_id="m2",
                chat_jid="g@g.us",
                sender="u@s",
                sender_name="Alice",
                content="world",
                timestamp="2026-02-20T10:00:01",
            )
        )

        await record_outbound("g@g.us", "hi back", "test", ["whatsapp"])

        result = await get_messaging_stats()
        assert result["total_inbound"] == 2
        assert result["total_outbound"] == 1
        assert result["last_received_at"] == "2026-02-20T10:00:01"
        assert result["last_sent_at"] is not None
        assert result["pending_deliveries"] == 1

    async def test_pending_deliveries_excludes_delivered(self):
        await store_chat_metadata("g@g.us", "2026-01-01T00:00:00", "Test")
        ledger_id = await record_outbound("g@g.us", "msg", "test", ["whatsapp", "slack"])
        await mark_delivered(ledger_id, "whatsapp")

        result = await get_messaging_stats()
        assert result["total_outbound"] == 1
        assert result["pending_deliveries"] == 1
