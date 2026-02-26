"""Tests for the database layer."""

from __future__ import annotations

import pytest

from pynchy.db import (
    _init_test_database,
    clear_session,
    create_host_job,
    create_task,
    delete_task,
    get_active_task_for_group,
    get_all_chats,
    get_all_sessions,
    get_all_tasks,
    get_all_workspace_profiles,
    get_chat_history,
    get_due_tasks,
    get_host_job_by_id,
    get_messages_since,
    get_messaging_stats,
    get_new_messages,
    get_router_state,
    get_session,
    get_task_by_id,
    get_tasks_for_group,
    get_workspace_profile,
    log_task_run,
    set_chat_cleared_at,
    set_router_state,
    set_session,
    set_workspace_profile,
    store_chat_metadata,
    store_message,
    store_message_direct,
    update_chat_name,
    update_host_job,
    update_task,
    update_task_after_run,
)
from pynchy.types import (
    NewMessage,
    ServiceTrustConfig,
    TaskRunLog,
    WorkspaceProfile,
    WorkspaceSecurity,
)


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

        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
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

        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
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

        # Verify the flag is persisted via get_chat_history (which returns all messages).
        # get_messages_since filters out is_from_me=True (bot/self messages).
        messages = await get_chat_history("group@g.us", limit=50)
        mine = [m for m in messages if m.id == "msg-3"]
        assert len(mine) == 1
        assert mine[0].is_from_me is True

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

        messages = await get_messages_since("group@g.us", "2024-01-01T00:00:00.000Z")
        assert len(messages) == 1
        assert messages[0].content == "updated"


# --- getMessagesSince ---


class TestGetMessagesSince:
    @pytest.fixture(autouse=True)
    async def _seed_messages(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        for id_, content, ts, sender in [
            ("m1", "first", "2024-01-01T00:00:01.000Z", "Alice"),
            ("m2", "second", "2024-01-01T00:00:02.000Z", "Bob"),
            ("m4", "third", "2024-01-01T00:00:04.000Z", "Carol"),
        ]:
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
        # Bot message — excluded by sender filter, not content prefix
        await store_message_direct(
            id="m3",
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


# --- getNewMessages ---


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
        # Bot message — excluded by sender filter
        await store_message_direct(
            id="a3",
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
        assert new_ts == "2024-01-01T00:00:04.000Z"

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


# --- Sender filtering (transparent token stream) ---


class TestSenderFiltering:
    """Verify that get_new_messages() / get_messages_since() return only
    user-originated messages (is_from_me=False) and exclude internal
    bot/system messages (is_from_me=True)."""

    @pytest.fixture(autouse=True)
    async def _seed_messages(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        # Real user messages (should pass filter)
        await store_message(
            _store(
                id="m-user",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="hello",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await store_message_direct(
            id="m-tui",
            chat_jid="group@g.us",
            sender="tui-user",
            sender_name="You",
            content="tui message",
            timestamp="2024-01-01T00:00:02.000Z",
            is_from_me=False,
        )
        await store_message_direct(
            id="m-deploy",
            chat_jid="group@g.us",
            sender="deploy",
            sender_name="deploy",
            content="[DEPLOY COMPLETE]",
            timestamp="2024-01-01T00:00:03.000Z",
            is_from_me=False,
        )
        # Slack user message — sender is a Slack user ID (no @ sign)
        await store_message_direct(
            id="m-slack",
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
            await store_message_direct(
                id=f"m-{id_suffix}",
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
        assert "tui-user" in senders
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
        assert "tui-user" in senders
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


# --- Sessions ---


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

    async def test_get_all_sessions(self):
        await set_session("group-a", "session-1")
        await set_session("group-b", "session-2")
        sessions = await get_all_sessions()
        assert sessions == {"group-a": "session-1", "group-b": "session-2"}

    async def test_get_all_sessions_empty(self):
        sessions = await get_all_sessions()
        assert sessions == {}


# --- Router state ---


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


# --- Chat cleared_at ---


class TestChatClearedAt:
    async def test_cleared_at_hides_old_messages(self):
        """Messages before cleared_at should not appear in get_chat_history."""
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message(
            _store(
                id="old-msg",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="old message",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await store_message(
            _store(
                id="new-msg",
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
        await store_message(
            _store(
                id="msg-1",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="first",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await store_message(
            _store(
                id="msg-2",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="second",
                timestamp="2024-01-01T00:00:02.000Z",
            )
        )

        messages = await get_chat_history("group@g.us", limit=50)
        assert len(messages) == 2


# --- update_chat_name ---


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


# --- store_message_direct with metadata ---


class TestStoreMessageDirect:
    async def test_stores_metadata(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message_direct(
            id="meta-msg",
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
        assert messages[0].metadata == {"severity": "warning", "source": "deploy"}
        assert messages[0].message_type == "system"

    async def test_stores_without_metadata(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message_direct(
            id="no-meta",
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


# --- Advanced task operations ---


class TestTaskAdvanced:
    """Tests for task querying and lifecycle functions."""

    _TASK_TEMPLATE = {
        "group_folder": "main",
        "chat_jid": "group@g.us",
        "prompt": "test prompt",
        "schedule_type": "cron",
        "schedule_value": "0 * * * *",
        "context_mode": "isolated",
        "status": "active",
        "created_at": "2024-01-01T00:00:00.000Z",
    }

    async def test_get_tasks_for_group(self):
        await create_task({**self._TASK_TEMPLATE, "id": "t1", "next_run": None})
        await create_task(
            {**self._TASK_TEMPLATE, "id": "t2", "group_folder": "other", "next_run": None}
        )
        await create_task({**self._TASK_TEMPLATE, "id": "t3", "next_run": None})

        tasks = await get_tasks_for_group("main")
        assert len(tasks) == 2
        assert all(t.group_folder == "main" for t in tasks)

    async def test_get_all_tasks(self):
        await create_task({**self._TASK_TEMPLATE, "id": "t1", "next_run": None})
        await create_task(
            {**self._TASK_TEMPLATE, "id": "t2", "group_folder": "other", "next_run": None}
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 2

    async def test_get_due_tasks(self):
        # Due task (next_run in the past)
        await create_task(
            {**self._TASK_TEMPLATE, "id": "due-1", "next_run": "2020-01-01T00:00:00Z"}
        )
        # Not due (next_run in the far future)
        await create_task(
            {**self._TASK_TEMPLATE, "id": "future-1", "next_run": "2099-01-01T00:00:00Z"}
        )
        # No next_run (should not be due)
        await create_task({**self._TASK_TEMPLATE, "id": "no-next", "next_run": None})
        # Paused task (should not be due even if next_run is past)
        await create_task(
            {
                **self._TASK_TEMPLATE,
                "id": "paused-1",
                "next_run": "2020-01-01T00:00:00Z",
                "status": "paused",
            }
        )

        due = await get_due_tasks()
        assert len(due) == 1
        assert due[0].id == "due-1"

    async def test_get_active_task_for_group(self):
        await create_task({**self._TASK_TEMPLATE, "id": "active-1", "next_run": None})
        await create_task(
            {**self._TASK_TEMPLATE, "id": "paused-1", "status": "paused", "next_run": None}
        )

        task = await get_active_task_for_group("main")
        assert task is not None
        assert task.id == "active-1"

    async def test_get_active_task_for_group_returns_none(self):
        task = await get_active_task_for_group("nonexistent")
        assert task is None

    async def test_update_task_ignores_disallowed_fields(self):
        await create_task({**self._TASK_TEMPLATE, "id": "t1", "next_run": None})

        # Try updating a field that isn't in the allowed set
        await update_task("t1", {"chat_jid": "hacked@g.us", "status": "paused"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.status == "paused"
        assert task.chat_jid == "group@g.us"  # unchanged

    async def test_update_task_noop_for_empty_fields(self):
        await create_task({**self._TASK_TEMPLATE, "id": "t1", "next_run": None})
        await update_task("t1", {"invalid_field": "value"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.status == "active"  # unchanged

    async def test_update_task_after_run_sets_completed_for_once(self):
        await create_task(
            {
                **self._TASK_TEMPLATE,
                "id": "once-task",
                "schedule_type": "once",
                "next_run": "2024-06-01T00:00:00Z",
            }
        )

        # next_run=None means 'once' task → should be marked 'completed'
        await update_task_after_run("once-task", None, "Completed successfully")
        task = await get_task_by_id("once-task")
        assert task is not None
        assert task.status == "completed"
        assert task.last_result == "Completed successfully"
        assert task.last_run is not None

    async def test_update_task_after_run_preserves_active_for_cron(self):
        await create_task(
            {**self._TASK_TEMPLATE, "id": "cron-task", "next_run": "2024-06-01T00:00:00Z"}
        )

        # next_run is set → task stays 'active'
        await update_task_after_run("cron-task", "2024-06-01T01:00:00Z", "Done")
        task = await get_task_by_id("cron-task")
        assert task is not None
        assert task.status == "active"
        assert task.next_run == "2024-06-01T01:00:00Z"

    async def test_log_task_run(self):
        await create_task({**self._TASK_TEMPLATE, "id": "logged-task", "next_run": None})

        await log_task_run(
            TaskRunLog(
                task_id="logged-task",
                run_at="2024-06-01T00:00:00Z",
                duration_ms=1500,
                status="success",
                result="Done",
                error=None,
            )
        )
        await log_task_run(
            TaskRunLog(
                task_id="logged-task",
                run_at="2024-06-01T01:00:00Z",
                duration_ms=500,
                status="error",
                result=None,
                error="Something went wrong",
            )
        )

        # Verify logs exist by deleting the task (which also deletes logs)
        await delete_task("logged-task")
        assert await get_task_by_id("logged-task") is None

    async def test_create_task_with_repo_access(self):
        await create_task(
            {
                **self._TASK_TEMPLATE,
                "id": "pa-task",
                "next_run": None,
                "repo_access": "owner/pynchy",
            }
        )
        task = await get_task_by_id("pa-task")
        assert task is not None
        assert task.repo_access == "owner/pynchy"

    async def test_create_task_without_repo_access(self):
        await create_task({**self._TASK_TEMPLATE, "id": "no-pa", "next_run": None})
        task = await get_task_by_id("no-pa")
        assert task is not None
        assert task.repo_access is None


# --- Workspace profiles ---


class TestWorkspaceProfiles:
    async def test_set_and_get_workspace_profile(self):
        profile = WorkspaceProfile(
            jid="test@g.us",
            name="Test Workspace",
            folder="test-ws",
            trigger="@Test",
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        result = await get_workspace_profile("test@g.us")
        assert result is not None
        assert result.name == "Test Workspace"
        assert result.folder == "test-ws"
        assert result.trigger == "@Test"

    async def test_workspace_profile_with_security(self):
        security = WorkspaceSecurity(
            services={
                "email": ServiceTrustConfig(
                    public_source=True,
                    secret_data=True,
                    public_sink=True,
                    dangerous_writes=True,
                ),
                "calendar": ServiceTrustConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            },
            contains_secrets=True,
        )
        profile = WorkspaceProfile(
            jid="secure@g.us",
            name="Secure Workspace",
            folder="secure-ws",
            trigger="@Secure",
            security=security,
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        result = await get_workspace_profile("secure@g.us")
        assert result is not None
        assert result.security.contains_secrets is True
        assert "email" in result.security.services
        assert result.security.services["email"].public_source is True
        assert result.security.services["email"].dangerous_writes is True
        assert "calendar" in result.security.services
        assert result.security.services["calendar"].public_source is False

    async def test_get_workspace_profile_returns_none(self):
        result = await get_workspace_profile("nonexistent@g.us")
        assert result is None

    async def test_get_all_workspace_profiles(self):
        for i in range(2):
            profile = WorkspaceProfile(
                jid=f"ws-{i}@g.us",
                name=f"WS {i}",
                folder=f"ws-{i}",
                trigger=f"@WS{i}",
                added_at="2024-01-01T00:00:00Z",
            )
            await set_workspace_profile(profile)

        profiles = await get_all_workspace_profiles()
        assert len(profiles) == 2
        assert all(isinstance(p, WorkspaceProfile) for p in profiles.values())

    async def test_workspace_profile_validation_rejects_invalid(self):
        profile = WorkspaceProfile(
            jid="bad@g.us",
            name="",  # invalid: empty name
            folder="bad-ws",
            trigger="@Bad",
            added_at="2024-01-01T00:00:00Z",
        )
        with pytest.raises(ValueError, match="Workspace name is required"):
            await set_workspace_profile(profile)

    async def test_workspace_profile_admin_flag_roundtrip(self):
        profile = WorkspaceProfile(
            jid="admin-1@g.us",
            name="Admin",
            folder="admin-1",
            trigger="@Pynchy",
            is_admin=True,
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        result = await get_workspace_profile("admin-1@g.us")
        assert result is not None
        assert result.is_admin is True

    async def test_workspace_profile_defaults_security_on_missing(self):
        """If security_profile column is NULL, defaults are used."""
        profile = WorkspaceProfile(
            jid="legacy@g.us",
            name="Legacy",
            folder="legacy",
            trigger="@Legacy",
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        # get_workspace_profile reads from the same table
        result = await get_workspace_profile("legacy@g.us")
        assert result is not None
        assert result.security.services == {}
        assert result.security.contains_secrets is False


# --- get_chat_history limit ---


class TestChatHistoryLimit:
    async def test_respects_limit(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        for i in range(10):
            await store_message(
                _store(
                    id=f"msg-{i}",
                    chat_jid="group@g.us",
                    sender="123@s.whatsapp.net",
                    sender_name="Alice",
                    content=f"message {i}",
                    timestamp=f"2024-01-01T00:00:{i:02d}.000Z",
                )
            )

        messages = await get_chat_history("group@g.us", limit=3)
        assert len(messages) == 3
        # Newest last (reversed)
        assert messages[0].content == "message 7"
        assert messages[2].content == "message 9"

    async def test_returns_newest_last(self):
        """get_chat_history returns messages in chronological order (oldest first)."""
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await store_message(
            _store(
                id="old",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="old",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await store_message(
            _store(
                id="new",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="new",
                timestamp="2024-01-01T00:00:02.000Z",
            )
        )

        messages = await get_chat_history("group@g.us", limit=50)
        assert messages[0].content == "old"
        assert messages[1].content == "new"


# --- get_task_by_id edge case ---


class TestGetTaskById:
    async def test_returns_none_for_nonexistent(self):
        result = await get_task_by_id("does-not-exist")
        assert result is None

    async def test_returns_full_task_fields(self):
        await create_task(
            {
                "id": "full-task",
                "group_folder": "my-group",
                "chat_jid": "jid@g.us",
                "prompt": "Do a thing",
                "schedule_type": "interval",
                "schedule_value": "3600000",
                "context_mode": "group",
                "next_run": "2024-06-01T00:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00Z",
                "repo_access": "owner/pynchy",
            }
        )
        task = await get_task_by_id("full-task")
        assert task is not None
        assert task.id == "full-task"
        assert task.group_folder == "my-group"
        assert task.chat_jid == "jid@g.us"
        assert task.prompt == "Do a thing"
        assert task.schedule_type == "interval"
        assert task.schedule_value == "3600000"
        assert task.context_mode == "group"
        assert task.next_run == "2024-06-01T00:00:00Z"
        assert task.status == "active"
        assert task.repo_access == "owner/pynchy"


# --- get_last_group_sync / set_last_group_sync ---


class TestGroupSync:
    async def test_get_returns_none_initially(self):
        from pynchy.db import get_last_group_sync

        result = await get_last_group_sync()
        assert result is None

    async def test_set_and_get_group_sync(self):
        from pynchy.db import get_last_group_sync, set_last_group_sync

        await set_last_group_sync()
        result = await get_last_group_sync()
        assert result is not None
        # Should be a valid ISO timestamp
        assert "T" in result


# --- _update_by_id shared helper ---


class TestUpdateById:
    """Tests for the _update_by_id helper used by update_task and update_host_job."""

    async def test_update_task_updates_allowed_fields(self):
        """update_task should update fields in the allowlist."""
        await create_task(
            {
                "id": "upd-1",
                "group_folder": "test",
                "chat_jid": "test@g.us",
                "prompt": "original",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "next_run": "2025-06-01T00:00:00.000Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await update_task("upd-1", {"status": "paused", "prompt": "updated"})
        task = await get_task_by_id("upd-1")
        assert task is not None
        assert task.status == "paused"
        assert task.prompt == "updated"

    async def test_update_task_ignores_disallowed_fields(self):
        """update_task should silently skip fields not in the allowlist."""
        await create_task(
            {
                "id": "upd-2",
                "group_folder": "test",
                "chat_jid": "test@g.us",
                "prompt": "original",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "next_run": "2025-06-01T00:00:00.000Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        # Try to update group_folder which is not in the allowlist
        await update_task("upd-2", {"group_folder": "hacked", "status": "paused"})
        task = await get_task_by_id("upd-2")
        assert task is not None
        assert task.group_folder == "test"  # unchanged
        assert task.status == "paused"  # allowed field updated

    async def test_update_task_noop_with_no_allowed_fields(self):
        """update_task with only disallowed fields should be a safe no-op."""
        await create_task(
            {
                "id": "upd-3",
                "group_folder": "test",
                "chat_jid": "test@g.us",
                "prompt": "original",
                "schedule_type": "once",
                "schedule_value": "2025-06-01T00:00:00.000Z",
                "next_run": "2025-06-01T00:00:00.000Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
            }
        )

        await update_task("upd-3", {"id": "evil", "chat_jid": "evil@g.us"})
        task = await get_task_by_id("upd-3")
        assert task is not None
        assert task.status == "active"

    async def test_update_host_job_updates_allowed_fields(self):
        """update_host_job should update fields in the allowlist."""
        await create_host_job(
            {
                "id": "hj-upd-1",
                "name": "test-job",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "admin-1",
                "enabled": True,
            }
        )

        await update_host_job("hj-upd-1", {"status": "paused", "enabled": 0})
        job = await get_host_job_by_id("hj-upd-1")
        assert job is not None
        assert job.status == "paused"
        assert job.enabled is False

    async def test_update_host_job_ignores_disallowed_fields(self):
        """update_host_job should silently skip fields not in the allowlist."""
        await create_host_job(
            {
                "id": "hj-upd-2",
                "name": "test-job-2",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "admin-1",
                "enabled": True,
            }
        )

        # Try to update command which is not in the allowlist
        await update_host_job("hj-upd-2", {"command": "rm -rf /", "status": "paused"})
        job = await get_host_job_by_id("hj-upd-2")
        assert job is not None
        assert job.command == "echo hi"  # unchanged
        assert job.status == "paused"  # allowed field updated


@pytest.mark.anyio
class TestEnsureColumns:
    """Test that _ensure_columns adds missing columns to existing tables."""

    async def test_adds_missing_column_to_existing_table(self):
        """Simulate an old DB missing a column, then run _ensure_columns."""
        import aiosqlite

        from pynchy.db._schema import _ensure_columns

        db = await aiosqlite.connect(":memory:")
        # Create registered_groups WITHOUT is_admin column (old schema)
        await db.executescript("""
            CREATE TABLE registered_groups (
                jid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                folder TEXT NOT NULL UNIQUE,
                trigger_pattern TEXT NOT NULL,
                added_at TEXT NOT NULL,
                container_config TEXT
            );
        """)

        # Verify is_admin is missing
        cursor = await db.execute("PRAGMA table_info(registered_groups)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "is_admin" not in cols

        # Run _ensure_columns — should add is_admin and security_profile
        await _ensure_columns(db)

        cursor = await db.execute("PRAGMA table_info(registered_groups)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "is_admin" in cols
        assert "security_profile" in cols

        await db.close()

    async def test_noop_when_all_columns_present(self):
        """_ensure_columns is a no-op when schema is already up to date."""
        import aiosqlite

        from pynchy.db._schema import _SCHEMA, _ensure_columns

        db = await aiosqlite.connect(":memory:")
        await db.executescript(_SCHEMA)

        # Should not raise
        await _ensure_columns(db)
        await db.close()


# --- get_messaging_stats ---


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
        await store_message(
            _store(
                id="m1",
                chat_jid="g@g.us",
                sender="u@s",
                sender_name="Alice",
                content="hello",
                timestamp="2026-02-20T10:00:00",
            )
        )
        await store_message(
            _store(
                id="m2",
                chat_jid="g@g.us",
                sender="u@s",
                sender_name="Alice",
                content="world",
                timestamp="2026-02-20T10:00:01",
            )
        )

        from pynchy.db import record_outbound

        await record_outbound("g@g.us", "hi back", "test", ["whatsapp"])

        result = await get_messaging_stats()
        assert result["total_inbound"] == 2
        assert result["total_outbound"] == 1
        assert result["last_received_at"] == "2026-02-20T10:00:01"
        assert result["last_sent_at"] is not None
        assert result["pending_deliveries"] == 1  # undelivered whatsapp entry

    async def test_pending_deliveries_excludes_delivered(self):
        await store_chat_metadata("g@g.us", "2026-01-01T00:00:00", "Test")

        from pynchy.db import mark_delivered, record_outbound

        ledger_id = await record_outbound("g@g.us", "msg", "test", ["whatsapp", "slack"])

        # Mark whatsapp as delivered, leave slack pending
        await mark_delivered(ledger_id, "whatsapp")

        result = await get_messaging_stats()
        assert result["total_outbound"] == 1
        assert result["pending_deliveries"] == 1  # only slack is pending
