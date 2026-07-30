"""Tests for the outbound ledger — record, pending, deliver, gc."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from conftest import init_test_database

from pynchy.state import (
    gc_delivered,
    get_pending_outbound,
    mark_delivered,
    mark_delivery_error,
    record_outbound,
    record_outbound_deliveries,
    store_chat_metadata,
)
from pynchy.state.outbound import OutboundDelivery, OutboundDeliveryOperation


class _MissingLedgerIdCursor:
    lastrowid = None


class _MissingLedgerIdDatabase:
    async def execute(self, *_args: object) -> _MissingLedgerIdCursor:
        return _MissingLedgerIdCursor()


class _AtomicWrite:
    async def __aenter__(self) -> _MissingLedgerIdDatabase:
        return _MissingLedgerIdDatabase()

    async def __aexit__(self, *_args: object) -> bool:
        return False


@pytest.fixture
async def _db():
    await init_test_database()
    # record_outbound has a FOREIGN KEY on chats(jid), seed a chat row
    await store_chat_metadata("group@g.us", "2024-01-01T00:00:00")


@pytest.mark.usefixtures("_db")
class TestRecordOutbound:
    @pytest.mark.asyncio
    async def test_returns_ledger_id(self):
        lid = await record_outbound("group@g.us", "hello", "broadcast", ["slack"])
        assert isinstance(lid, int)
        assert lid > 0

    @pytest.mark.asyncio
    async def test_creates_delivery_rows_for_each_channel(self):
        lid = await record_outbound("group@g.us", "hello", "broadcast", ["slack", "whatsapp"])
        # Both channels should have pending deliveries
        slack_pending = await get_pending_outbound("slack", "group@g.us")
        wa_pending = await get_pending_outbound("whatsapp", "group@g.us")

        assert len(slack_pending) == 1
        assert len(wa_pending) == 1
        assert slack_pending[0].ledger_id == lid
        assert wa_pending[0].content == "hello"

    @pytest.mark.asyncio
    async def test_preserves_edit_operation_and_remote_message_id(self):
        await record_outbound_deliveries(
            "group@g.us",
            "accumulated trace",
            "agent_trace",
            [
                OutboundDelivery(
                    channel_name="discord",
                    operation=OutboundDeliveryOperation.EDIT,
                    remote_message_id="discord-123",
                )
            ],
        )

        pending = await get_pending_outbound("discord", "group@g.us")

        assert pending[0].operation is OutboundDeliveryOperation.EDIT
        assert pending[0].remote_message_id == "discord-123"


@pytest.mark.usefixtures("_db")
class TestGetPendingOutbound:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_pending(self):
        result = await get_pending_outbound("slack", "group@g.us")
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_pending_in_creation_order(self):
        await record_outbound("group@g.us", "first", "broadcast", ["slack"])
        await record_outbound("group@g.us", "second", "broadcast", ["slack"])

        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 2
        assert pending[0].content == "first"
        assert pending[1].content == "second"

    @pytest.mark.asyncio
    async def test_excludes_delivered(self):
        lid = await record_outbound("group@g.us", "msg", "broadcast", ["slack"])
        await mark_delivered(lid, "slack")

        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_includes_errored_without_delivery(self):
        """Errored deliveries (delivered_at IS NULL) are still pending for retry."""
        lid = await record_outbound("group@g.us", "msg", "broadcast", ["slack"])
        await mark_delivery_error(lid, "slack", "timeout")

        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_scoped_to_channel(self):
        await record_outbound("group@g.us", "msg", "broadcast", ["slack", "whatsapp"])

        await mark_delivered(
            (await get_pending_outbound("slack", "group@g.us"))[0].ledger_id, "slack"
        )

        # Slack is delivered, whatsapp still pending
        assert len(await get_pending_outbound("slack", "group@g.us")) == 0
        assert len(await get_pending_outbound("whatsapp", "group@g.us")) == 1


@pytest.mark.usefixtures("_db")
class TestMarkDelivered:
    @pytest.mark.asyncio
    async def test_clears_error_on_success(self):
        lid = await record_outbound("group@g.us", "msg", "broadcast", ["slack"])
        await mark_delivery_error(lid, "slack", "first attempt failed")
        await mark_delivered(lid, "slack")

        # No longer pending
        assert len(await get_pending_outbound("slack", "group@g.us")) == 0


@pytest.mark.usefixtures("_db")
class TestGcDelivered:
    @pytest.mark.asyncio
    async def test_deletes_old_fully_delivered(self):
        lid = await record_outbound("group@g.us", "msg", "broadcast", ["slack"])
        await mark_delivered(lid, "slack")

        # Advance gc's clock so the just-recorded entry is past max_age
        with patch("pynchy.state.outbound.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2030, 1, 1, tzinfo=UTC)
            deleted = await gc_delivered(max_age_hours=1)
        assert deleted == 1

    @pytest.mark.asyncio
    async def test_preserves_recent_entries(self):
        lid = await record_outbound("group@g.us", "msg", "broadcast", ["slack"])
        await mark_delivered(lid, "slack")

        # Not backdated — should survive gc
        deleted = await gc_delivered(max_age_hours=1)
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_preserves_pending_entries(self):
        await record_outbound("group@g.us", "msg", "broadcast", ["slack"])

        # Old enough for gc, but undelivered — must be preserved
        with patch("pynchy.state.outbound.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2030, 1, 1, tzinfo=UTC)
            deleted = await gc_delivered(max_age_hours=1)
        assert deleted == 0


@pytest.mark.asyncio
async def test_record_outbound_fails_when_insert_returns_no_ledger_id() -> None:
    with (
        patch("pynchy.state.outbound.atomic_write", return_value=_AtomicWrite()),
        patch("pynchy.state.outbound.ensure_chat_parent", new=AsyncMock()),
        pytest.raises(RuntimeError, match="did not return a row id"),
    ):
        await record_outbound_deliveries("group@g.us", "hello", "test", [])
