"""Tests for the database layer."""

from __future__ import annotations

import pytest

from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    clear_session,
    create_task,
    delete_task,
    get_all_chats,
    get_all_sessions,
    get_chat_history,
    get_messages_since,
    get_new_messages,
    get_router_state,
    get_session,
    get_session_security_taint,
    get_task_by_id,
    mark_session_security_taint,
    set_router_state,
    set_session,
    store_chat_metadata,
    store_message,
    update_task,
    upgrade_message_cursor,
)
from tests.state_support import (
    _store,
    _store_message_row,
    _store_message_row_direct,
)

pytest_plugins = ("tests.state_support",)


class TestStoreMessage:
    async def test_stores_a_message_and_retrieves_it(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="msg-1",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="hello world",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )

        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
        assert len(messages) == 1
        assert messages[0].id == "msg-1"
        assert messages[0].sender == "123@s.whatsapp.net"
        assert messages[0].sender_name == "Alice"
        assert messages[0].content == "hello world"

    async def test_stores_empty_content(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="msg-2",
                chat_jid="group@g.us",
                sender="111@s.whatsapp.net",
                sender_name="Dave",
                content="",
                timestamp="2024-01-01T00:00:04.000Z",
            )
        )

        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
        assert len(messages) == 1
        assert not messages[0].content

    async def test_message_cursors_follow_local_ingestion_order(self):
        timestamp = "2024-01-01T00:00:01.000Z"
        await _store_message_row(
            _store(
                message_id="first",
                chat_jid="group@g.us",
                sender="alice",
                sender_name="Alice",
                content="first",
                timestamp=timestamp,
            )
        )
        first_batch, cursor = await get_new_messages(["group@g.us"], "")
        assert [message.id for message in first_batch] == ["first"]

        for message_id, delayed_timestamp in (
            ("same-second", timestamp),
            ("delayed", "2023-12-31T23:59:59.000Z"),
        ):
            await _store_message_row(
                _store(
                    message_id=message_id,
                    chat_jid="group@g.us",
                    sender="alice",
                    sender_name="Alice",
                    content=message_id,
                    timestamp=delayed_timestamp,
                )
            )

        new_batch, _ = await get_new_messages(["group@g.us"], cursor)
        pending = await get_messages_since("group@g.us", cursor)

        assert [message.id for message in new_batch] == ["same-second", "delayed"]
        assert [message.id for message in pending] == ["same-second", "delayed"]

    async def test_duplicate_store_keeps_original_ingestion_position(self):
        message = _store(
            message_id="duplicate",
            chat_jid="group@g.us",
            sender="alice",
            sender_name="Alice",
            content="first",
            timestamp="2024-01-01T00:00:01.000Z",
        )
        await store_message(message)
        _, cursor = await get_new_messages([message.chat_jid], "")

        message.content = "updated"
        message.timestamp = "2024-01-01T00:00:02.000Z"
        await store_message(message)

        new_batch, _ = await get_new_messages([message.chat_jid], cursor)
        assert not new_batch

    async def test_legacy_cursor_upgrade_tracks_delayed_messages(self):
        timestamp = "2024-01-01T00:00:01.000Z"
        await _store_message_row(
            _store(
                message_id="processed",
                chat_jid="group@g.us",
                sender="alice",
                sender_name="Alice",
                content="processed",
                timestamp=timestamp,
            )
        )
        cursor = await upgrade_message_cursor(["group@g.us"], timestamp)
        await _store_message_row(
            _store(
                message_id="delayed",
                chat_jid="group@g.us",
                sender="alice",
                sender_name="Alice",
                content="delayed",
                timestamp="2023-12-31T23:59:59.000Z",
            )
        )

        pending = await get_messages_since("group@g.us", cursor)
        assert [message.id for message in pending] == ["delayed"]

    async def test_legacy_cursor_upgrade_recovers_already_stranded_delayed_messages(self):
        timestamp = "2024-01-01T00:00:01.000Z"
        await _store_message_row(
            _store(
                message_id="processed",
                chat_jid="group@g.us",
                sender="alice",
                sender_name="Alice",
                content="processed",
                timestamp=timestamp,
            )
        )
        await _store_message_row(
            _store(
                message_id="already-delayed",
                chat_jid="group@g.us",
                sender="alice",
                sender_name="Alice",
                content="delayed",
                timestamp="2023-12-31T23:59:59.000Z",
            )
        )
        await _store_message_row(
            _store(
                message_id="same-timestamp",
                chat_jid="group@g.us",
                sender="alice",
                sender_name="Alice",
                content="same timestamp",
                timestamp=timestamp,
            )
        )

        cursor = await upgrade_message_cursor(["group@g.us"], timestamp)

        pending = await get_messages_since("group@g.us", cursor)
        assert [message.id for message in pending] == ["already-delayed", "same-timestamp"]

    async def test_stores_metadata(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="msg-meta",
                chat_jid="group@g.us",
                sender="111@s.whatsapp.net",
                sender_name="Dave",
                content="with attachment",
                timestamp="2024-01-01T00:00:04.000Z",
                metadata={"attachments": [{"filename": "voice.ogg", "content_type": "audio/ogg"}]},
            )
        )

        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
        assert len(messages) == 1
        assert messages[0].metadata
        assert messages[0].metadata["attachments"] == [
            {"filename": "voice.ogg", "content_type": "audio/ogg"}
        ]

    async def test_stores_is_from_me_flag(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="msg-3",
                chat_jid="group@g.us",
                sender="me@s.whatsapp.net",
                sender_name="Me",
                content="my message",
                timestamp="2024-01-01T00:00:05.000Z",
                is_from_me=True,
            )
        )

        # Verify the flag is persisted via get_chat_history (which returns all messages).
        # get_messages_since filters out is_from_me=True (bot/self messages).
        messages = await get_chat_history("group@g.us", limit=50)
        mine = [m for m in messages if m.id == "msg-3"]
        assert len(mine) == 1
        assert mine[0].is_from_me is True

    async def test_upserts_on_duplicate_id_chat_jid(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="msg-dup",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="original",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await _store_message_row(
            _store(
                message_id="msg-dup",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="updated",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )

        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
        assert len(messages) == 1
        assert messages[0].content == "updated"


class TestGetMessagesSince:
    @pytest.fixture(autouse=True)
    async def _seed_messages(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        for id_, content, ts, sender in [
            ("m1", "first", "2024-01-01T00:00:01.000Z", "Alice"),
            ("m2", "second", "2024-01-01T00:00:02.000Z", "Bob"),
            ("m4", "third", "2024-01-01T00:00:04.000Z", "Carol"),
        ]:
            await _store_message_row(
                _store(
                    message_id=id_,
                    chat_jid="group@g.us",
                    sender=f"{sender}@s.whatsapp.net",
                    sender_name=sender,
                    content=content,
                    timestamp=ts,
                )
            )
        # Bot message — excluded by sender filter, not content prefix
        await _store_message_row_direct(
            message_id="m3",
            chat_jid="group@g.us",
            sender="bot",
            sender_name="pynchy",
            content="bot reply",
            timestamp="2024-01-01T00:00:03.000Z",
            is_from_me=True,
        )

    async def test_returns_messages_after_timestamp(self):
        msgs = await get_messages_since("group@g.us", "2024-01-01T00:00:02.000Z")
        # Excludes m1, m2 (before/at timestamp), m3 (bot — sender filter)
        assert len(msgs) == 1
        assert msgs[0].content == "third"

    async def test_excludes_bot_messages(self):
        msgs = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
        bot_msgs = [m for m in msgs if m.sender == "bot"]
        assert len(bot_msgs) == 0

    async def test_returns_all_messages_when_empty_timestamp(self):
        msgs = await get_messages_since("group@g.us", "")
        # 3 user messages (bot message excluded by sender filter)
        assert len(msgs) == 3

    async def test_returns_all_messages_when_timestamp_is_none(self):
        msgs = await get_messages_since("group@g.us", None)
        # 3 user messages (bot message excluded by sender filter)
        assert len(msgs) == 3


class TestGetNewMessages:
    @pytest.fixture(autouse=True)
    async def _seed_messages(self):
        await store_chat_metadata("group1@g.us", "2024-01-01T00:00:00.000Z")
        await store_chat_metadata("group2@g.us", "2024-01-01T00:00:00.000Z")
        for id_, chat, content, ts in [
            ("a1", "group1@g.us", "g1 msg1", "2024-01-01T00:00:01.000Z"),
            ("a2", "group2@g.us", "g2 msg1", "2024-01-01T00:00:02.000Z"),
            ("a4", "group1@g.us", "g1 msg2", "2024-01-01T00:00:04.000Z"),
        ]:
            await _store_message_row(
                _store(
                    message_id=id_,
                    chat_jid=chat,
                    sender="user@s.whatsapp.net",
                    sender_name="User",
                    content=content,
                    timestamp=ts,
                )
            )
        # Bot message — excluded by sender filter
        await _store_message_row_direct(
            message_id="a3",
            chat_jid="group1@g.us",
            sender="bot",
            sender_name="pynchy",
            content="reply",
            timestamp="2024-01-01T00:00:03.000Z",
            is_from_me=True,
        )

    async def test_returns_new_messages_across_multiple_groups(self):
        messages, new_ts = await get_new_messages(
            ["group1@g.us", "group2@g.us"],
            "2024-01-01T00:00:00.000Z",
        )
        assert len(messages) == 3
        replay, replay_cursor = await get_new_messages(["group1@g.us", "group2@g.us"], new_ts)
        assert not replay
        assert replay_cursor == new_ts

    async def test_filters_by_timestamp(self):
        messages, _ = await get_new_messages(
            ["group1@g.us", "group2@g.us"],
            "2024-01-01T00:00:02.000Z",
        )
        assert len(messages) == 1
        assert messages[0].content == "g1 msg2"

    async def test_returns_empty_for_no_groups(self):
        messages, new_ts = await get_new_messages([], "")
        assert len(messages) == 0
        assert not new_ts


class TestStoreChatMetadata:
    async def test_stores_chat_with_jid_as_default_name(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        chats = await get_all_chats()
        assert len(chats) == 1
        assert chats[0]["jid"] == "group@g.us"
        assert chats[0]["name"] == "group@g.us"

    async def test_stores_chat_with_explicit_name(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z", "My Group")
        chats = await get_all_chats()
        assert chats[0]["name"] == "My Group"

    async def test_updates_name_on_subsequent_call(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:01.000Z", "Updated Name")
        chats = await get_all_chats()
        assert len(chats) == 1
        assert chats[0]["name"] == "Updated Name"

    async def test_preserves_newer_timestamp(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:05.000Z")
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:01.000Z")
        chats = await get_all_chats()
        assert chats[0]["last_message_time"] == "2024-01-01T00:00:05.000Z"


class TestTaskCRUD:
    async def test_creates_and_retrieves_a_task(self):
        await create_task(
            ScheduledTask(
                id="task-1",
                group_folder="main",
                chat_jid="group@g.us",
                prompt="do something",
                schedule_type="once",
                schedule_value="2024-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2024-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
                memory_enabled=False,
            )
        )

        task = await get_task_by_id("task-1")
        assert task is not None
        assert task.prompt == "do something"
        assert task.status == "active"
        assert task.memory_enabled is False

    async def test_updates_task_status(self):
        await create_task(
            ScheduledTask(
                id="task-2",
                group_folder="main",
                chat_jid="group@g.us",
                prompt="test",
                schedule_type="once",
                schedule_value="2024-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run=None,
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await update_task("task-2", {"status": "paused"})
        task = await get_task_by_id("task-2")
        assert task is not None
        assert task.status == "paused"

    async def test_deletes_task_and_run_logs(self):
        await create_task(
            ScheduledTask(
                id="task-3",
                group_folder="main",
                chat_jid="group@g.us",
                prompt="delete me",
                schedule_type="once",
                schedule_value="2024-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run=None,
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await delete_task("task-3")
        assert await get_task_by_id("task-3") is None


class TestSenderFiltering:
    """Verify that get_new_messages() / get_messages_since() return only
    user-originated messages (is_from_me=False) and exclude internal
    bot/system messages (is_from_me=True)."""

    @pytest.fixture(autouse=True)
    async def _seed_messages(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        # Real user messages (should pass filter)
        await _store_message_row(
            _store(
                message_id="m-user",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="hello",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await _store_message_row_direct(
            message_id="m-local",
            chat_jid="group@g.us",
            sender="local-user",
            sender_name="You",
            content="local message",
            timestamp="2024-01-01T00:00:02.000Z",
            is_from_me=False,
        )
        await _store_message_row_direct(
            message_id="m-deploy",
            chat_jid="group@g.us",
            sender="deploy",
            sender_name="deploy",
            content="[DEPLOY COMPLETE]",
            timestamp="2024-01-01T00:00:03.000Z",
            is_from_me=False,
        )
        # Slack user message — sender is a Slack user ID (no @ sign)
        await _store_message_row_direct(
            message_id="m-slack",
            chat_jid="group@g.us",
            sender="U07ABC123",
            sender_name="Bob",
            content="slack message",
            timestamp="2024-01-01T00:00:03.500Z",
            is_from_me=False,
        )
        # Internal senders (should be excluded)
        for sender, id_suffix in [
            ("thinking", "think"),
            ("tool_use", "tool"),
            ("tool_result", "toolr"),
            ("system", "sys"),
            ("result_meta", "meta"),
            ("host", "host"),
            ("bot", "bot"),
        ]:
            await _store_message_row_direct(
                message_id=f"m-{id_suffix}",
                chat_jid="group@g.us",
                sender=sender,
                sender_name=sender,
                content=f"{sender} content",
                timestamp=f"2024-01-01T00:00:04.{id_suffix}Z",
                is_from_me=True,
            )

    async def test_get_new_messages_only_returns_user_senders(self):
        messages, _ = await get_new_messages(["group@g.us"], "2024-01-01T00:00:00.000Z")
        senders = {m.sender for m in messages}
        assert "123@s.whatsapp.net" in senders
        assert "local-user" in senders
        assert "deploy" in senders
        assert "U07ABC123" in senders  # Slack user ID
        # Internal senders excluded
        for internal in (
            "thinking",
            "tool_use",
            "tool_result",
            "system",
            "result_meta",
            "host",
            "bot",
        ):
            assert internal not in senders

    async def test_get_messages_since_only_returns_user_senders(self):
        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
        senders = {m.sender for m in messages}
        assert "123@s.whatsapp.net" in senders
        assert "local-user" in senders
        assert "deploy" in senders
        assert "U07ABC123" in senders  # Slack user ID
        for internal in (
            "thinking",
            "tool_use",
            "tool_result",
            "system",
            "result_meta",
            "host",
            "bot",
        ):
            assert internal not in senders

    async def test_get_chat_history_includes_all_types(self):
        """Chat history (UI display) should include all message types."""
        messages = await get_chat_history("group@g.us", limit=50)
        senders = {m.sender for m in messages}
        assert "123@s.whatsapp.net" in senders
        assert "bot" in senders
        assert "host" in senders
        assert "thinking" in senders
        assert "tool_use" in senders


class TestSessions:
    async def test_set_and_get_session(self):
        await set_session("my-group", "session-abc")
        result = await get_session("my-group")
        assert result == "session-abc"

    async def test_get_session_returns_none_when_missing(self):
        result = await get_session("nonexistent")
        assert result is None

    async def test_set_session_upserts(self):
        await set_session("my-group", "session-1")
        await set_session("my-group", "session-2")
        result = await get_session("my-group")
        assert result == "session-2"

    async def test_clear_session(self):
        await set_session("my-group", "session-abc")
        await clear_session("my-group")
        result = await get_session("my-group")
        assert result is None

    async def test_clear_session_noop_when_missing(self):
        """Clearing a nonexistent session should not raise."""
        await clear_session("nonexistent")

    async def test_security_taint_is_sticky_until_session_reset(self):
        await set_session("my-group", "session-abc")

        first = await mark_session_security_taint(
            "my-group",
            corruption_tainted=True,
        )
        continued = await mark_session_security_taint(
            "my-group",
            secret_tainted=True,
        )
        trusted_input = await mark_session_security_taint("my-group")

        assert first.corruption_tainted is True
        assert first.secret_tainted is False
        assert continued.corruption_tainted is True
        assert continued.secret_tainted is True
        assert trusted_input == continued

        await clear_session("my-group")

        assert await get_session("my-group") is None
        assert await get_session_security_taint("my-group") == type(first)()

    async def test_get_all_sessions(self):
        await set_session("group-a", "session-1")
        await set_session("group-b", "session-2")
        sessions = await get_all_sessions()
        assert sessions == {"group-a": "session-1", "group-b": "session-2"}

    async def test_get_all_sessions_empty(self):
        sessions = await get_all_sessions()
        assert sessions == {}


class TestRouterState:
    async def test_set_and_get_router_state(self):
        await set_router_state("last_timestamp", "2024-01-01T00:00:00Z")
        result = await get_router_state("last_timestamp")
        assert result == "2024-01-01T00:00:00Z"

    async def test_get_router_state_returns_none_when_missing(self):
        result = await get_router_state("nonexistent_key")
        assert result is None

    async def test_set_router_state_upserts(self):
        await set_router_state("key", "value-1")
        await set_router_state("key", "value-2")
        result = await get_router_state("key")
        assert result == "value-2"
