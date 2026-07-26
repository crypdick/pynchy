"""Tests for the database layer."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import aiosqlite
import pytest
from freezegun import freeze_time

from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.state import (
    begin_in_flight_turn,
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
    get_host_job_by_id,
    get_in_flight_turn_for_task,
    get_last_group_sync,
    get_latest_inbound_timestamp,
    get_messages_since,
    get_messaging_stats,
    get_new_messages,
    get_router_state,
    get_session,
    get_session_security_taint,
    get_task_by_id,
    get_task_run_logs,
    get_tasks_for_group,
    get_workspace_profile,
    init_test_database,
    log_task_run,
    mark_delivered,
    mark_session_security_taint,
    rebind_workspace_profile,
    record_outbound,
    record_task_completion,
    resume_task,
    set_chat_cleared_at,
    set_last_group_sync,
    set_router_state,
    set_session,
    set_workspace_profile,
    store_chat_metadata,
    store_message_direct,
    update_chat_name,
    update_host_job,
    update_task,
)
from pynchy.state.connection import atomic_write
from pynchy.state.schema import create_schema
from pynchy.types import (
    InFlightTurn,
    InFlightWorkKind,
    NewMessage,
    ScheduledTask,
    ServiceTrustConfig,
    SessionPolicy,
    TaskRunLog,
    WorkspaceProfile,
    WorkspaceSecurity,
)


@pytest.fixture(autouse=True)
async def _setup_db():
    await init_test_database()


def _store(
    *,
    message_id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool = False,
    metadata: dict[str, object] | None = None,
) -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        is_from_me=is_from_me,
        metadata=metadata,
    )


async def _store_message_row(msg: NewMessage, message_type: str = "user") -> None:
    await _store_message_row_direct(
        message_id=msg.id,
        chat_jid=msg.chat_jid,
        sender=msg.sender,
        sender_name=msg.sender_name,
        content=msg.content,
        timestamp=msg.timestamp,
        is_from_me=msg.is_from_me or False,
        message_type=message_type,
        metadata=msg.metadata,
    )


async def _store_message_row_direct(
    *,
    message_id: str,
    chat_jid: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool,
    message_type: str = "user",
    metadata: dict[str, object] | None = None,
) -> None:
    await store_message_direct(
        message_id=message_id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        is_from_me=is_from_me,
        message_type=message_type,
        metadata=metadata,
    )


def _full_task() -> ScheduledTask:
    return ScheduledTask(
        id="full-task",
        group_folder="my-group",
        chat_jid="jid@g.us",
        prompt="Do a thing",
        schedule_type="interval",
        schedule_value="3600000",
        session_policy=SessionPolicy.CONTINUE,
        next_run="2024-06-01T00:00:00Z",
        status="active",
        created_at="2024-01-01T00:00:00Z",
        repo_access="owner/pynchy",
    )


def _assert_full_task(task: ScheduledTask) -> None:
    assert (
        task.id,
        task.group_folder,
        task.chat_jid,
        task.prompt,
        task.schedule_type,
        task.schedule_value,
        task.session_policy,
        task.next_run,
        task.status,
        task.repo_access,
    ) == (
        "full-task",
        "my-group",
        "jid@g.us",
        "Do a thing",
        "interval",
        "3600000",
        SessionPolicy.CONTINUE,
        None,
        "active",
        "owner/pynchy",
    )


# --- storeMessage ---


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
        assert not new_ts


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
            )
        )

        task = await get_task_by_id("task-1")
        assert task is not None
        assert task.prompt == "do something"
        assert task.status == "active"

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


# --- Sender filtering (transparent token stream) ---


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


# --- Advanced task operations ---


class TestTaskAdvanced:
    """Tests for task querying and lifecycle functions."""

    _TASK_TEMPLATE = ScheduledTask(
        id="",
        group_folder="main",
        chat_jid="group@g.us",
        prompt="test prompt",
        schedule_type="cron",
        schedule_value="0 * * * *",
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        status="active",
        created_at="2024-01-01T00:00:00.000Z",
    )

    async def test_get_tasks_for_group(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))
        await create_task(
            replace(self._TASK_TEMPLATE, id="t2", group_folder="other", next_run=None)
        )
        await create_task(replace(self._TASK_TEMPLATE, id="t3", next_run=None))

        tasks = await get_tasks_for_group("main")
        assert len(tasks) == 2
        assert all(t.group_folder == "main" for t in tasks)

    async def test_get_all_tasks(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))
        await create_task(
            replace(self._TASK_TEMPLATE, id="t2", group_folder="other", next_run=None)
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 2

    async def test_get_active_task_for_group(self):
        await create_task(replace(self._TASK_TEMPLATE, id="active-1", next_run=None))
        await create_task(
            replace(self._TASK_TEMPLATE, id="paused-1", status="paused", next_run=None)
        )

        task = await get_active_task_for_group("main")
        assert task is not None
        assert task.id == "active-1"

    async def test_get_active_task_for_group_returns_none(self):
        task = await get_active_task_for_group("nonexistent")
        assert task is None

    async def test_delete_task_clears_unfinished_checkpoint(self):
        task = replace(self._TASK_TEMPLATE, id="cancelled-task", next_run=None)
        await create_task(task)
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="cancelled-turn",
                chat_jid=task.chat_jid,
                group_folder=task.group_folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[{"content": task.prompt}],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2024-01-01T00:00:00Z",
                task_id=task.id,
            )
        )

        await delete_task(task.id)

        assert await get_in_flight_turn_for_task(task.id) is None

    async def test_update_task_ignores_disallowed_fields(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))

        # Try updating a field that isn't in the allowed set
        await update_task("t1", {"invalid_field": "hacked", "status": "paused"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.status == "paused"
        assert not hasattr(task, "invalid_field")

    async def test_update_task_allows_chat_jid(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))

        await update_task("t1", {"chat_jid": "new@g.us"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.chat_jid == "new@g.us"

    async def test_update_task_noop_for_empty_fields(self):
        await create_task(replace(self._TASK_TEMPLATE, id="t1", next_run=None))
        await update_task("t1", {"invalid_field": "value"})
        task = await get_task_by_id("t1")
        assert task is not None
        assert task.status == "active"  # unchanged

    async def test_record_task_completion_sets_completed_for_once(self):
        await create_task(
            replace(
                self._TASK_TEMPLATE,
                id="once-task",
                schedule_type="once",
                next_run="2024-06-01T00:00:00Z",
            )
        )

        await record_task_completion(
            "once-task", last_result="Completed successfully", completed=True
        )
        task = await get_task_by_id("once-task")
        assert task is not None
        assert task.status == "completed"
        assert task.last_result == "Completed successfully"
        assert task.last_run is not None

    async def test_record_task_completion_preserves_recurring_schedule_state(self):
        await create_task(
            replace(self._TASK_TEMPLATE, id="cron-task", next_run="2024-06-01T00:00:00Z")
        )

        await record_task_completion("cron-task", last_result="Done", completed=False)
        task = await get_task_by_id("cron-task")
        assert task is not None
        assert task.status == "active"
        assert task.next_run is None

    async def test_log_task_run(self):
        await create_task(replace(self._TASK_TEMPLATE, id="logged-task", next_run=None))

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

    async def test_log_task_run_persists_occurrence_and_temporal_run_metadata(self):
        await create_task(replace(self._TASK_TEMPLATE, id="attempt-task", next_run=None))

        await log_task_run(
            TaskRunLog(
                task_id="attempt-task",
                run_at="2024-06-01T00:00:00Z",
                duration_ms=500,
                status="error",
                result=None,
                error="ValueError: failed on port 12345",
                temporal_workflow_id="workflow-1",
                temporal_workflow_run_id="workflow-run-1",
                temporal_attempt=2,
                turn_id="turn-1",
                error_signature="ValueError: failed on port #",
                escalation_reason="stagnation",
            )
        )

        logs = await get_task_run_logs("attempt-task", limit=1)

        assert len(logs) == 1
        assert logs[0].temporal_workflow_id == "workflow-1"
        assert logs[0].temporal_workflow_run_id == "workflow-run-1"
        assert logs[0].temporal_attempt == 2
        assert logs[0].turn_id == "turn-1"
        assert logs[0].error_signature == "ValueError: failed on port #"
        assert logs[0].escalation_reason == "stagnation"

    async def test_resume_task_preserves_history_and_resets_failure_window(self):
        await create_task(
            replace(self._TASK_TEMPLATE, id="resume-task", next_run=None, status="paused")
        )
        await log_task_run(
            TaskRunLog(
                task_id="resume-task",
                run_at="2024-06-01T00:00:00Z",
                duration_ms=500,
                status="error",
                error="persistent failure",
            )
        )

        await resume_task("resume-task")

        task = await get_task_by_id("resume-task")
        logs = await get_task_run_logs("resume-task")
        assert task is not None
        assert task.status == "active"
        assert task.schedule_value == self._TASK_TEMPLATE.schedule_value
        assert task.occurrence_generation == 0
        assert [log.status for log in logs] == ["resumed", "error"]
        assert logs[0].temporal_workflow_run_id is None
        assert logs[0].turn_id is None

    async def test_once_task_resume_creates_one_fresh_occurrence_under_race(self):
        original = replace(
            self._TASK_TEMPLATE,
            id="resume-once",
            schedule_type="once",
            schedule_value="2026-07-25T05:16:14+00:00",
            status="paused",
            repo_access="crypdick/pynchy",
            conversation_id="conv-linear-issue",
        )
        previous_workflow_id = agent_task_workflow_id(original)
        await create_task(original)
        # Temporal treats the workflow's PAUSED outcome as a successful completion,
        # while Pynchy records the circuit-breaker reason as task error evidence.
        await log_task_run(
            TaskRunLog(
                task_id=original.id,
                run_at="2026-07-25T05:16:14+00:00",
                duration_ms=1,
                status="error",
                error="Same error repeated",
                temporal_workflow_id=previous_workflow_id,
                temporal_workflow_run_id="successful-paused-run",
                escalation_reason="stagnation",
            )
        )

        resumed_at = "2026-07-26T06:00:00+00:00"
        with freeze_time(resumed_at):
            await asyncio.gather(
                resume_task(original.id),
                resume_task(original.id),
                resume_task(original.id),
            )

        resumed = await get_task_by_id(original.id)
        logs = await get_task_run_logs(original.id)
        assert resumed is not None
        assert resumed.status == "active"
        assert resumed.schedule_value == original.schedule_value
        assert resumed.occurrence_due_at == resumed_at
        assert resumed.occurrence_generation == 1
        assert resumed.superseded_occurrence_due_at == original.schedule_value
        assert resumed.superseded_occurrence_generation == 0
        assert resumed.repo_access == original.repo_access
        assert resumed.conversation_id == original.conversation_id
        resumed_workflow_id = agent_task_workflow_id(resumed)
        assert resumed_workflow_id != previous_workflow_id
        assert resumed_workflow_id.endswith("-resume-1")
        assert [log.status for log in logs] == ["resumed", "error"]

        await resume_task(original.id)

        unchanged = await get_task_by_id(original.id)
        assert unchanged is not None
        assert unchanged.occurrence_generation == 1
        assert len(await get_task_run_logs(original.id)) == 2

        await update_task(original.id, {"status": "paused"})
        with freeze_time(resumed_at):
            await resume_task(original.id)

        resumed_again = await get_task_by_id(original.id)
        assert resumed_again is not None
        assert resumed_again.schedule_value == original.schedule_value
        assert resumed_again.occurrence_due_at == resumed_at
        assert resumed_again.occurrence_generation == 2
        assert resumed_again.superseded_occurrence_due_at == resumed_at
        assert resumed_again.superseded_occurrence_generation == 1
        assert agent_task_workflow_id(resumed_again) != resumed_workflow_id
        assert agent_task_workflow_id(resumed_again).endswith("-resume-2")

    async def test_resume_task_ignores_missing_and_non_paused_rows(self):
        completed = replace(
            self._TASK_TEMPLATE,
            id="completed-once",
            schedule_type="once",
            schedule_value="2026-07-25T05:16:14+00:00",
            status="completed",
        )
        await create_task(completed)

        await resume_task(completed.id)
        await resume_task("missing-task")

        unchanged = await get_task_by_id(completed.id)
        assert unchanged is not None
        assert unchanged.status == "completed"
        assert unchanged.schedule_value == completed.schedule_value
        assert unchanged.occurrence_generation == 0
        assert unchanged.occurrence_due_at is None
        assert unchanged.superseded_occurrence_generation is None
        assert unchanged.superseded_occurrence_due_at is None
        assert await get_task_run_logs(completed.id) == []

    async def test_create_task_with_repo_access(self):
        await create_task(
            replace(
                self._TASK_TEMPLATE,
                id="pa-task",
                next_run=None,
                repo_access="owner/pynchy",
            )
        )
        task = await get_task_by_id("pa-task")
        assert task is not None
        assert task.repo_access == "owner/pynchy"

    async def test_create_task_without_repo_access(self):
        await create_task(replace(self._TASK_TEMPLATE, id="no-pa", next_run=None))
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
            cop_active=False,
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
        assert result.security.cop_active is False
        assert "email" in result.security.services
        assert result.security.services["email"].public_source is True
        assert result.security.services["email"].dangerous_writes is True
        assert "calendar" in result.security.services
        assert result.security.services["calendar"].public_source is False

    async def test_get_workspace_profile_returns_none(self):
        result = await get_workspace_profile("nonexistent@g.us")
        assert result is None

    async def test_duplicate_jid_or_folder_ownership_fails_closed(self):
        original = WorkspaceProfile(
            jid="discord:channel:one",
            name="Original",
            folder="one",
            trigger="@Pynchy",
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(original)

        with pytest.raises(ValueError, match="already owned by workspace"):
            await set_workspace_profile(replace(original, folder="two"))
        with pytest.raises(ValueError, match="use explicit rebind"):
            await set_workspace_profile(replace(original, jid="discord:channel:two"))

        assert await get_all_workspace_profiles() == {original.jid: original}

    async def test_explicit_workspace_rebind_atomically_replaces_jid(self):
        original = WorkspaceProfile(
            jid="discord:channel:old",
            name="Original",
            folder="one",
            trigger="@Pynchy",
            added_at="2024-01-01T00:00:00Z",
        )
        replacement = replace(
            original,
            jid="discord:channel:new",
            name="Replacement",
        )
        await set_workspace_profile(original)

        old_jid = await rebind_workspace_profile(replacement)

        assert old_jid == original.jid
        assert await get_workspace_profile(original.jid) is None
        assert await get_all_workspace_profiles() == {replacement.jid: replacement}

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
        assert result.security.cop_active is True

    async def test_get_workspace_profile_raises_on_corrupt_security(self):
        """A corrupt security_profile must fail loud, not silently default trust."""
        async with atomic_write() as db:
            await db.execute(
                """INSERT OR REPLACE INTO registered_groups -- temporal-ok
                    (jid, name, folder, trigger_pattern, added_at,
                     container_config, security_profile, is_admin)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "corrupt@g.us",
                    "Corrupt",
                    "corrupt",
                    "@Corrupt",
                    "2024-01-01T00:00:00Z",
                    None,
                    "{not valid json",
                    0,
                ),
            )

        with pytest.raises(ValueError, match="Corrupt security_profile"):
            await get_workspace_profile("corrupt@g.us")


# --- get_chat_history limit ---


class TestChatHistoryLimit:
    async def test_respects_limit(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        for i in range(10):
            await _store_message_row(
                _store(
                    message_id=f"msg-{i}",
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
        await _store_message_row(
            _store(
                message_id="old",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="old",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await _store_message_row(
            _store(
                message_id="new",
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
        await create_task(_full_task())
        task = await get_task_by_id("full-task")
        assert task is not None
        _assert_full_task(task)


# --- get_last_group_sync / set_last_group_sync ---


class TestGroupSync:
    async def test_get_returns_none_initially(self):
        result = await get_last_group_sync()
        assert result is None

    async def test_set_and_get_group_sync(self):
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
            ScheduledTask(
                id="upd-1",
                group_folder="test",
                chat_jid="test@g.us",
                prompt="original",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await update_task("upd-1", {"status": "paused", "prompt": "updated"})
        task = await get_task_by_id("upd-1")
        assert task is not None
        assert task.status == "paused"
        assert task.prompt == "updated"

    async def test_update_task_ignores_disallowed_fields(self):
        """update_task should silently skip fields not in the allowlist."""
        await create_task(
            ScheduledTask(
                id="upd-2",
                group_folder="test",
                chat_jid="test@g.us",
                prompt="original",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
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
            ScheduledTask(
                id="upd-3",
                group_folder="test",
                chat_jid="test@g.us",
                prompt="original",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
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

    async def test_adds_requester_delivery_turn_without_losing_executions(self):
        """Existing execution owners survive the new delivery correlation column."""
        db = await aiosqlite.connect(":memory:")
        await create_schema(db)
        await db.execute(
            """
            INSERT INTO work_item_executions (
                id, workspace, linear_issue_id, linear_issue_identifier,
                linear_issue_url, turn_id, attempt, initiated_by,
                observed_state_id, observed_state_name, status, evidence_refs,
                requester_delivery_status, created_at, updated_at
            ) VALUES (
                'execution-1', 'pynchy', 'issue-1', 'SYN-1',
                'https://linear.app/example/issue/SYN-1', 'owner-turn', 1,
                'linear-webhook:test', 'state-in-progress', 'In Progress',
                'in_progress', '[]', 'not_requested',
                '2026-07-26T00:00:00Z', '2026-07-26T00:00:00Z'
            )
            """
        )
        await db.execute("ALTER TABLE work_item_executions DROP COLUMN requester_delivery_turn_id")

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(work_item_executions)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "requester_delivery_turn_id" in columns
        cursor = await db.execute(
            "SELECT turn_id, requester_delivery_turn_id "
            "FROM work_item_executions WHERE id = 'execution-1'"
        )
        assert await cursor.fetchone() == ("owner-turn", None)
        await db.close()

    async def test_migrates_stale_work_item_outcomes_without_losing_blocker_evidence(self):
        """Existing terminal projections clear only after preserving transition evidence."""
        db = await aiosqlite.connect(":memory:")
        await create_schema(db)
        await db.executescript(
            """
            INSERT INTO work_item_executions (
                id, workspace, linear_issue_id, linear_issue_identifier,
                linear_issue_url, attempt, initiated_by, observed_state_id,
                observed_state_name, status, summary, blocker, handoff_to,
                evidence_refs, requester_delivery_status, created_at, updated_at
            ) VALUES
                (
                    'completed-execution', 'pynchy', 'issue-completed', 'SYN-88',
                    'https://linear.app/example/issue/SYN-88', 1, 'linear-webhook:test',
                    'state-done', 'Done', 'completed', 'Publication succeeded.',
                    'GitHub permission missing', 'release operator', '[]', 'delivered',
                    '2026-07-26T00:00:00Z', '2026-07-26T00:10:00Z'
                ),
                (
                    'blocked-execution', 'pynchy', 'issue-blocked', 'SYN-99',
                    'https://linear.app/example/issue/SYN-99', 1, 'linear-webhook:test',
                    'state-blocked', 'Blocked', 'blocked', 'Deployment is blocked.',
                    'Deployment credential missing', 'release operator', '[]', 'delivered',
                    '2026-07-26T00:00:00Z', '2026-07-26T00:10:00Z'
                );
            INSERT INTO work_item_transitions (
                execution_id, request_id, operation, target_status,
                result_execution_status, evidence_refs, status, created_at, resolved_at
            ) VALUES
                (
                    'completed-execution', 'blocked-completed', 'move_to_blocked', 'blocked',
                    'blocked', '[]', 'succeeded',
                    '2026-07-26T00:01:00Z', '2026-07-26T00:01:01Z'
                ),
                (
                    'blocked-execution', 'blocked-current', 'move_to_blocked', 'blocked',
                    'blocked', '[]', 'succeeded',
                    '2026-07-26T00:01:00Z', '2026-07-26T00:01:01Z'
                );
            """
        )
        await db.execute("ALTER TABLE work_item_transitions DROP COLUMN summary")
        await db.execute("ALTER TABLE work_item_transitions DROP COLUMN blocker")
        await db.execute("ALTER TABLE work_item_transitions DROP COLUMN handoff_to")

        await create_schema(db)

        cursor = await db.execute(
            """
            SELECT status, blocker, handoff_to
            FROM work_item_executions
            ORDER BY id
            """
        )
        assert await cursor.fetchall() == [
            ("blocked", "Deployment credential missing", "release operator"),
            ("completed", None, None),
        ]
        cursor = await db.execute(
            """
            SELECT request_id, summary, blocker, handoff_to
            FROM work_item_transitions
            ORDER BY request_id
            """
        )
        assert await cursor.fetchall() == [
            (
                "blocked-completed",
                None,
                "GitHub permission missing",
                "release operator",
            ),
            (
                "blocked-current",
                None,
                "Deployment credential missing",
                "release operator",
            ),
        ]
        await db.close()

    async def test_adds_occurrence_state_to_existing_scheduled_tasks(self):
        """Existing one-shot tasks gain the initial generation without row loss."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                group_folder TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                next_run TEXT,
                last_run TEXT,
                last_result TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                session_policy TEXT NOT NULL DEFAULT 'reset_before_run',
                repo_access TEXT,
                input_source TEXT NOT NULL DEFAULT 'scheduled_task',
                config_job_name TEXT,
                derived_thread_name TEXT,
                bound_chat_jid TEXT,
                bound_group_folder TEXT,
                conversation_id TEXT,
                last_reset_occurrence TEXT
            );
            INSERT INTO scheduled_tasks (
                id, group_folder, chat_jid, prompt, schedule_type,
                schedule_value, status, created_at
            ) VALUES (
                'legacy-once', 'admin', 'slack:CADMIN', 'Continue work',
                'once', '2026-07-25T05:16:14+00:00', 'paused',
                '2026-07-25T04:45:00+00:00'
            );
        """)

        await create_schema(db)

        cursor = await db.execute(
            "SELECT occurrence_generation, occurrence_due_at, "
            "superseded_occurrence_generation, superseded_occurrence_due_at "
            "FROM scheduled_tasks WHERE id = 'legacy-once'"
        )
        assert await cursor.fetchone() == (0, None, None, None)
        await db.close()

    async def test_adds_missing_column_to_existing_table(self):
        """Simulate an old DB missing a column, then run create_schema."""
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

        # create_schema is the public entry that runs the _ensure_columns
        # migration; on an old table it should add is_admin and security_profile.
        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(registered_groups)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "is_admin" in cols
        assert "security_profile" in cols

        await db.close()

    async def test_adds_active_control_state_to_existing_in_flight_turns(self):
        """Old checkpoints migrate to the active control state without row loss."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE in_flight_turns (
                turn_id TEXT PRIMARY KEY,
                chat_jid TEXT NOT NULL,
                group_folder TEXT NOT NULL,
                work_kind TEXT NOT NULL,
                input_messages TEXT NOT NULL,
                input_start_cursor TEXT NOT NULL,
                input_end_cursor TEXT NOT NULL,
                started_at TEXT NOT NULL,
                task_id TEXT,
                session_id TEXT,
                output_sent INTEGER NOT NULL DEFAULT 0,
                interrupted_at TEXT,
                deploy_id TEXT,
                claimed_at TEXT,
                scheduled_base_chat_jid TEXT,
                scheduled_thread_slot INTEGER,
                conversation_claim_id TEXT,
                input_source TEXT NOT NULL DEFAULT 'user'
            );
            INSERT INTO in_flight_turns (
                turn_id, chat_jid, group_folder, work_kind, input_messages,
                input_start_cursor, input_end_cursor, started_at,
                scheduled_base_chat_jid, scheduled_thread_slot
            ) VALUES (
                'legacy-turn', 'slack:C123', 'admin', 'interactive', '[]',
                '', 'cursor', '2026-07-25T10:00:00+00:00',
                'slack:legacy-parent', 7
            );
        """)

        await create_schema(db)

        cursor = await db.execute(
            "SELECT turn_id, control_state FROM in_flight_turns WHERE turn_id = 'legacy-turn'"
        )
        assert await cursor.fetchone() == ("legacy-turn", "active")
        cursor = await db.execute("PRAGMA table_info(in_flight_turns)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "scheduled_base_chat_jid" not in columns
        assert "scheduled_thread_slot" not in columns
        await db.close()

    async def test_adds_task_run_identity_columns_to_existing_ledger(self):
        """Startup migration preserves old rows while adding explicit run identity."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE task_run_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                run_at TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT,
                temporal_workflow_id TEXT,
                temporal_attempt INTEGER,
                error_signature TEXT,
                escalation_reason TEXT
            );
            INSERT INTO task_run_logs (
                task_id, run_at, duration_ms, status, temporal_workflow_id, temporal_attempt
            ) VALUES ('task-1', '2026-07-22T00:00:00Z', 10, 'success', 'workflow-1', 1);
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(task_run_logs)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert {"temporal_workflow_run_id", "turn_id"} <= cols
        cursor = await db.execute(
            "SELECT temporal_workflow_id, temporal_workflow_run_id, turn_id "
            "FROM task_run_logs WHERE task_id = 'task-1'"
        )
        assert await cursor.fetchone() == ("workflow-1", None, None)
        await db.close()

    async def test_adds_delivery_operation_to_existing_outbound_ledger(self):
        """Existing pending sends migrate to explicit post semantics."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE outbound_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_jid TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL
            );
            CREATE TABLE outbound_deliveries (
                ledger_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                delivered_at TEXT,
                error TEXT,
                PRIMARY KEY (ledger_id, channel_name)
            );
            INSERT INTO outbound_ledger (
                chat_jid, content, timestamp, source
            ) VALUES (
                'discord:channel:1', 'pending', '2026-07-25T00:00:00Z', 'agent'
            );
            INSERT INTO outbound_deliveries (
                ledger_id, channel_name
            ) VALUES (1, 'discord');
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(outbound_deliveries)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert {"operation", "remote_message_id"} <= cols
        cursor = await db.execute(
            "SELECT operation, remote_message_id FROM outbound_deliveries WHERE ledger_id = 1"
        )
        assert await cursor.fetchone() == ("post", None)
        await db.close()

    async def test_noop_when_all_columns_present(self):
        """create_schema is idempotent when the schema is already up to date."""
        db = await aiosqlite.connect(":memory:")
        # First application builds the full schema; the second must not raise.
        await create_schema(db)
        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(registered_groups)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "is_admin" in cols
        await db.close()

    async def test_replaces_cached_task_thread_columns_with_config_job_provenance(self):
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                group_folder TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                next_run TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                persistent_thread_name TEXT,
                persistent_thread_jid TEXT
            );
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(scheduled_tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "config_job_name" in cols
        assert "persistent_thread_name" not in cols
        assert "persistent_thread_jid" not in cols
        await db.close()

    async def test_migrates_context_modes_once_then_drops_legacy_column(self):
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                group_folder TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                next_run TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                context_mode TEXT NOT NULL
            );
            INSERT INTO scheduled_tasks VALUES (
                'continued', 'group', 'group@g.us', 'continue', 'cron', '* * * * *',
                NULL, 'active', '2026-07-25T00:00:00Z', 'group'
            );
            INSERT INTO scheduled_tasks VALUES (
                'reset', 'group', 'group@g.us', 'reset', 'cron', '* * * * *',
                NULL, 'active', '2026-07-25T00:00:00Z', 'isolated'
            );
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(scheduled_tasks)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "context_mode" not in cols
        cursor = await db.execute("SELECT id, session_policy FROM scheduled_tasks ORDER BY id")
        assert await cursor.fetchall() == [
            ("continued", "continue"),
            ("reset", "reset_before_run"),
        ]
        await create_schema(db)
        await db.close()

    async def test_renames_conversation_event_phoenix_ref(self):
        """create_schema migrates old projection refs to provider-neutral names."""
        db = await aiosqlite.connect(":memory:")
        await db.executescript("""
            CREATE TABLE conversation_events (
                event_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL,
                chat_jid TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                kind TEXT NOT NULL,
                sender TEXT NOT NULL,
                sender_name TEXT,
                message_type TEXT NOT NULL,
                source_message_id TEXT,
                content_preview TEXT NOT NULL,
                phoenix_ref TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO conversation_events (
                event_id, turn_id, chat_jid, timestamp, kind, sender,
                message_type, content_preview, phoenix_ref
            ) VALUES (
                'evt_1', 'turn_1', 'slack:C123', '2026-07-10T00:00:00+00:00',
                'user_message', 'alice', 'user', 'hello', 'legacy:event:evt_1'
            );
        """)

        await create_schema(db)

        cursor = await db.execute("PRAGMA table_info(conversation_events)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "trace_ref" in cols
        assert "phoenix_ref" not in cols

        cursor = await db.execute(
            "SELECT trace_ref FROM conversation_events WHERE event_id = 'evt_1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "legacy:event:evt_1"
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
        assert result["pending_deliveries"] == 1  # undelivered whatsapp entry

    async def test_pending_deliveries_excludes_delivered(self):
        await store_chat_metadata("g@g.us", "2026-01-01T00:00:00", "Test")

        ledger_id = await record_outbound("g@g.us", "msg", "test", ["whatsapp", "slack"])

        # Mark whatsapp as delivered, leave slack pending
        await mark_delivered(ledger_id, "whatsapp")

        result = await get_messaging_stats()
        assert result["total_outbound"] == 1
        assert result["pending_deliveries"] == 1  # only slack is pending


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
