"""Tests for the database layer."""

from __future__ import annotations

from pynchy.state import (
    get_all_chats,
    get_chat_cleared_at,
    get_chat_history,
    set_chat_cleared_at,
    store_chat_metadata,
    update_chat_name,
)
from tests.state_support import (
    _store,
    _store_message_row,
    _store_message_row_direct,
)

pytest_plugins = ("tests.state_support",)


class TestChatClearedAt:
    async def test_unknown_chat_has_no_clear_boundary(self):
        assert await get_chat_cleared_at("missing@g.us") is None

    async def test_cleared_at_hides_old_messages(self):
        """Messages before cleared_at should not appear in get_chat_history."""
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="old-msg",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="old message",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await _store_message_row(
            _store(
                message_id="new-msg",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="new message",
                timestamp="2024-01-01T00:00:05.000Z",
            )
        )

        await set_chat_cleared_at("group@g.us", "2024-01-01T00:00:03.000Z")

        messages = await get_chat_history("group@g.us", limit=50)
        assert len(messages) == 1
        assert messages[0].content == "new message"

    async def test_no_cleared_at_returns_all(self):
        """Without cleared_at, all messages are returned."""
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="msg-1",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="first",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await _store_message_row(
            _store(
                message_id="msg-2",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="second",
                timestamp="2024-01-01T00:00:02.000Z",
            )
        )

        messages = await get_chat_history("group@g.us", limit=50)
        assert len(messages) == 2


class TestUpdateChatName:
    async def test_updates_existing_chat_name(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z", "Old Name")
        await update_chat_name("group@g.us", "New Name")
        chats = await get_all_chats()
        assert chats[0]["name"] == "New Name"

    async def test_creates_chat_if_not_exists(self):
        await update_chat_name("new@g.us", "Brand New")
        chats = await get_all_chats()
        assert len(chats) == 1
        assert chats[0]["name"] == "Brand New"


class TestStoreMessageDirect:
    async def test_stores_metadata(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row_direct(
            message_id="meta-msg",
            chat_jid="group@g.us",
            sender="123@s.whatsapp.net",
            sender_name="Alice",
            content="with metadata",
            timestamp="2024-01-01T00:00:01.000Z",
            is_from_me=False,
            message_type="system",
            metadata={"severity": "warning", "source": "deploy"},
        )

        messages = await get_chat_history("group@g.us", limit=50)
        assert len(messages) == 1
        assert messages[0].metadata
        assert messages[0].metadata["severity"] == "warning"
        assert messages[0].metadata["source"] == "deploy"
        assert messages[0].message_type == "system"

    async def test_stores_without_metadata(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row_direct(
            message_id="no-meta",
            chat_jid="group@g.us",
            sender="123@s.whatsapp.net",
            sender_name="Alice",
            content="no metadata",
            timestamp="2024-01-01T00:00:01.000Z",
            is_from_me=False,
        )

        messages = await get_chat_history("group@g.us", limit=50)
        assert len(messages) == 1
        assert messages[0].metadata is None
