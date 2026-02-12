"""Tests for the database layer.

Port of src/db.test.ts.
"""

from __future__ import annotations

import pytest

from nanoclawpy.db import (
    _init_test_database,
    create_task,
    delete_task,
    get_all_chats,
    get_messages_since,
    get_new_messages,
    get_task_by_id,
    store_chat_metadata,
    store_message,
    update_task,
)
from nanoclawpy.types import NewMessage


@pytest.fixture(autouse=True)
async def _setup_db():
    await _init_test_database()


def _store(
    *,
    id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool = False,
) -> NewMessage:
    return NewMessage(
        id=id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        is_from_me=is_from_me,
    )


# --- storeMessage ---


class TestStoreMessage:
    async def test_stores_a_message_and_retrieves_it(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message(
            _store(
                id="msg-1",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="hello world",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )

        messages = await get_messages_since(
            "group@g.us", "2024-01-01T00:00:00.000Z", "BotName"
        )
        assert len(messages) == 1
        assert messages[0].id == "msg-1"
        assert messages[0].sender == "123@s.whatsapp.net"
        assert messages[0].sender_name == "Alice"
        assert messages[0].content == "hello world"

    async def test_stores_empty_content(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message(
            _store(
                id="msg-2",
                chat_jid="group@g.us",
                sender="111@s.whatsapp.net",
                sender_name="Dave",
                content="",
                timestamp="2024-01-01T00:00:04.000Z",
            )
        )

        messages = await get_messages_since(
            "group@g.us", "2024-01-01T00:00:00.000Z", "BotName"
        )
        assert len(messages) == 1
        assert messages[0].content == ""

    async def test_stores_is_from_me_flag(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message(
            _store(
                id="msg-3",
                chat_jid="group@g.us",
                sender="me@s.whatsapp.net",
                sender_name="Me",
                content="my message",
                timestamp="2024-01-01T00:00:05.000Z",
                is_from_me=True,
            )
        )

        messages = await get_messages_since(
            "group@g.us", "2024-01-01T00:00:00.000Z", "BotName"
        )
        assert len(messages) == 1

    async def test_upserts_on_duplicate_id_chat_jid(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message(
            _store(
                id="msg-dup",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="original",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await store_message(
            _store(
                id="msg-dup",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="updated",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )

        messages = await get_messages_since(
            "group@g.us", "2024-01-01T00:00:00.000Z", "BotName"
        )
        assert len(messages) == 1
        assert messages[0].content == "updated"


# --- getMessagesSince ---


class TestGetMessagesSince:
    @pytest.fixture(autouse=True)
    async def _seed_messages(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        msgs = [
            ("m1", "first", "2024-01-01T00:00:01.000Z", "Alice"),
            ("m2", "second", "2024-01-01T00:00:02.000Z", "Bob"),
            ("m3", "Andy: bot reply", "2024-01-01T00:00:03.000Z", "Bot"),
            ("m4", "third", "2024-01-01T00:00:04.000Z", "Carol"),
        ]
        for id_, content, ts, sender in msgs:
            await store_message(
                _store(
                    id=id_,
                    chat_jid="group@g.us",
                    sender=f"{sender}@s.whatsapp.net",
                    sender_name=sender,
                    content=content,
                    timestamp=ts,
                )
            )

    async def test_returns_messages_after_timestamp(self):
        msgs = await get_messages_since(
            "group@g.us", "2024-01-01T00:00:02.000Z", "Andy"
        )
        # Excludes m1, m2 (before/at timestamp), m3 (bot message)
        assert len(msgs) == 1
        assert msgs[0].content == "third"

    async def test_excludes_assistant_messages(self):
        msgs = await get_messages_since(
            "group@g.us", "2024-01-01T00:00:00.000Z", "Andy"
        )
        bot_msgs = [m for m in msgs if m.content.startswith("Andy:")]
        assert len(bot_msgs) == 0

    async def test_returns_all_messages_when_empty_timestamp(self):
        msgs = await get_messages_since("group@g.us", "", "Andy")
        # 3 user messages (bot message excluded)
        assert len(msgs) == 3


# --- getNewMessages ---


class TestGetNewMessages:
    @pytest.fixture(autouse=True)
    async def _seed_messages(self):
        await store_chat_metadata("group1@g.us", "2024-01-01T00:00:00.000Z")
        await store_chat_metadata("group2@g.us", "2024-01-01T00:00:00.000Z")
        msgs = [
            ("a1", "group1@g.us", "g1 msg1", "2024-01-01T00:00:01.000Z"),
            ("a2", "group2@g.us", "g2 msg1", "2024-01-01T00:00:02.000Z"),
            ("a3", "group1@g.us", "Andy: reply", "2024-01-01T00:00:03.000Z"),
            ("a4", "group1@g.us", "g1 msg2", "2024-01-01T00:00:04.000Z"),
        ]
        for id_, chat, content, ts in msgs:
            await store_message(
                _store(
                    id=id_,
                    chat_jid=chat,
                    sender="user@s.whatsapp.net",
                    sender_name="User",
                    content=content,
                    timestamp=ts,
                )
            )

    async def test_returns_new_messages_across_multiple_groups(self):
        messages, new_ts = await get_new_messages(
            ["group1@g.us", "group2@g.us"],
            "2024-01-01T00:00:00.000Z",
            "Andy",
        )
        assert len(messages) == 3
        assert new_ts == "2024-01-01T00:00:04.000Z"

    async def test_filters_by_timestamp(self):
        messages, _ = await get_new_messages(
            ["group1@g.us", "group2@g.us"],
            "2024-01-01T00:00:02.000Z",
            "Andy",
        )
        assert len(messages) == 1
        assert messages[0].content == "g1 msg2"

    async def test_returns_empty_for_no_groups(self):
        messages, new_ts = await get_new_messages([], "", "Andy")
        assert len(messages) == 0
        assert new_ts == ""


# --- storeChatMetadata ---


class TestStoreChatMetadata:
    async def test_stores_chat_with_jid_as_default_name(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        chats = await get_all_chats()
        assert len(chats) == 1
        assert chats[0]["jid"] == "group@g.us"
        assert chats[0]["name"] == "group@g.us"

    async def test_stores_chat_with_explicit_name(self):
        await store_chat_metadata(
            "group@g.us", "2024-01-01T00:00:00.000Z", "My Group"
        )
        chats = await get_all_chats()
        assert chats[0]["name"] == "My Group"

    async def test_updates_name_on_subsequent_call(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_chat_metadata(
            "group@g.us", "2024-01-01T00:00:01.000Z", "Updated Name"
        )
        chats = await get_all_chats()
        assert len(chats) == 1
        assert chats[0]["name"] == "Updated Name"

    async def test_preserves_newer_timestamp(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:05.000Z")
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:01.000Z")
        chats = await get_all_chats()
        assert chats[0]["last_message_time"] == "2024-01-01T00:00:05.000Z"


# --- Task CRUD ---


class TestTaskCRUD:
    async def test_creates_and_retrieves_a_task(self):
        await create_task(
            {
                "id": "task-1",
                "group_folder": "main",
                "chat_jid": "group@g.us",
                "prompt": "do something",
                "schedule_type": "once",
                "schedule_value": "2024-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": "2024-06-01T00:00:00.000Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        task = await get_task_by_id("task-1")
        assert task is not None
        assert task.prompt == "do something"
        assert task.status == "active"

    async def test_updates_task_status(self):
        await create_task(
            {
                "id": "task-2",
                "group_folder": "main",
                "chat_jid": "group@g.us",
                "prompt": "test",
                "schedule_type": "once",
                "schedule_value": "2024-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": None,
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await update_task("task-2", {"status": "paused"})
        task = await get_task_by_id("task-2")
        assert task is not None
        assert task.status == "paused"

    async def test_deletes_task_and_run_logs(self):
        await create_task(
            {
                "id": "task-3",
                "group_folder": "main",
                "chat_jid": "group@g.us",
                "prompt": "delete me",
                "schedule_type": "once",
                "schedule_value": "2024-06-01T00:00:00.000Z",
                "context_mode": "isolated",
                "next_run": None,
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await delete_task("task-3")
        assert await get_task_by_id("task-3") is None
