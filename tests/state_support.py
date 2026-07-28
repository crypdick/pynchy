"""Tests for the database layer."""

from __future__ import annotations

import pytest

from pynchy.plugins.api import NewMessage
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    init_test_database,
    store_message_direct,
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
