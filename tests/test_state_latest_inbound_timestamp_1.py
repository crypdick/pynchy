"""Tests for the database layer."""

from __future__ import annotations

from pynchy.state import (
    get_latest_inbound_timestamp,
)
from tests.state_support import (
    _store,
    _store_message_row,
)

pytest_plugins = ("tests.state_support",)


class TestLatestInboundTimestamp:
    async def test_aggregates_selected_chats_without_outbound_rows(self):
        await _store_message_row(
            _store(
                message_id="selected-old",
                chat_jid="selected@g.us",
                sender="u@s",
                sender_name="Alice",
                content="private body",
                timestamp="2026-02-20T10:00:00",
            )
        )
        await _store_message_row(
            _store(
                message_id="selected-outbound",
                chat_jid="selected@g.us",
                sender="agent",
                sender_name="Agent",
                content="outbound",
                timestamp="2026-02-20T10:00:03",
                is_from_me=True,
            )
        )
        await _store_message_row(
            _store(
                message_id="other-new",
                chat_jid="other@g.us",
                sender="u@s",
                sender_name="Bob",
                content="other private body",
                timestamp="2026-02-20T10:00:04",
            )
        )

        assert await get_latest_inbound_timestamp(["selected@g.us"]) == ("2026-02-20T10:00:00")

    async def test_empty_selection_has_no_freshness_evidence(self):
        assert await get_latest_inbound_timestamp([]) is None
