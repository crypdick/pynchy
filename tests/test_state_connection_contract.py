"""Contract tests for state initialization."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state import (
    close_test_database,
    create_task,
    get_all_chats,
    get_task_by_id,
    init_database,
)
from pynchy.state.connection import StateRuntimeConfig, atomic_write

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.asyncio
async def test_state_database_access_requires_initialization() -> None:
    close_test_database()
    close_test_database()

    with pytest.raises(RuntimeError, match="Database not initialized"):
        await get_all_chats()


@pytest.mark.asyncio
async def test_init_database_uses_explicit_runtime_config(tmp_path: Path) -> None:
    database_path = tmp_path / "explicit" / "messages.db"
    close_test_database()

    try:
        await init_database(StateRuntimeConfig(database_path=database_path))

        assert await get_all_chats() == []
        assert database_path.is_file()
    finally:
        close_test_database()


@pytest.mark.asyncio
async def test_atomic_write_rolls_back_task_cancellation(tmp_path: Path) -> None:
    database_path = tmp_path / "cancelled-write.db"
    close_test_database()

    try:
        await init_database(StateRuntimeConfig(database_path=database_path))
        try:
            async with atomic_write() as database:
                await database.execute("INSERT INTO chats (jid) VALUES ('abandoned')")
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            pass

        async with atomic_write() as database:
            await database.execute("INSERT INTO chats (jid) VALUES ('committed')")

        assert {chat["jid"] for chat in await get_all_chats()} == {"committed"}
    finally:
        close_test_database()


@pytest.mark.asyncio
async def test_cancelled_transaction_cannot_rollback_a_competing_task_write(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "competing-write.db"
    close_test_database()
    holder_started = asyncio.Event()
    task_writer_waiting = asyncio.Event()
    never = asyncio.Event()
    task = ScheduledTask(
        id="preserved",
        group_folder="project",
        chat_jid="discord:channel:project",
        prompt="Deliver issue.",
        schedule_type="once",
        schedule_value="2026-07-30T20:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        created_at="2026-07-30T19:00:00+00:00",
    )

    async def hold_cancelled_transaction() -> None:
        async with atomic_write() as database:
            await database.execute("INSERT INTO chats (jid) VALUES ('abandoned')")
            holder_started.set()
            await never.wait()

    @asynccontextmanager
    async def observed_atomic_write() -> AsyncIterator[object]:
        task_writer_waiting.set()
        async with atomic_write() as database:
            yield database

    try:
        await init_database(StateRuntimeConfig(database_path=database_path))
        holder = asyncio.create_task(hold_cancelled_transaction())
        await holder_started.wait()
        with patch("pynchy.state.tasks.atomic_write", observed_atomic_write):
            writer = asyncio.create_task(create_task(task))
            await task_writer_waiting.wait()
            assert not writer.done()

            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder
            await writer

        assert await get_task_by_id(task.id) == task
        assert await get_all_chats() == []
    finally:
        close_test_database()
