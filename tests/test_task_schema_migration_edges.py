"""Coverage for legacy scheduled-task thread-column migration failures."""

from __future__ import annotations

import aiosqlite
import pytest

from pynchy.state.task_schema_migrations import migrate_cached_task_thread_binding


@pytest.mark.asyncio
async def test_migration_preserves_bindings_and_logs_drop_failures() -> None:
    async with aiosqlite.connect(":memory:") as database:
        await database.executescript(
            """
            CREATE TABLE scheduled_tasks (
                id TEXT PRIMARY KEY,
                group_folder TEXT NOT NULL,
                persistent_thread_jid TEXT,
                persistent_thread_name TEXT,
                bound_chat_jid TEXT,
                bound_group_folder TEXT,
                derived_thread_name TEXT
            );
            INSERT INTO scheduled_tasks (
                id, group_folder, persistent_thread_jid, persistent_thread_name
            ) VALUES ('task-1', 'project', 'slack:C123', 'Build thread');
            CREATE TRIGGER keep_legacy_name
            AFTER UPDATE OF persistent_thread_name ON scheduled_tasks
            BEGIN
                SELECT NEW.persistent_thread_name;
            END;
            CREATE TRIGGER keep_legacy_jid
            AFTER UPDATE OF persistent_thread_jid ON scheduled_tasks
            BEGIN
                SELECT NEW.persistent_thread_jid;
            END;
            """
        )

        await migrate_cached_task_thread_binding(database)

        cursor = await database.execute(
            "SELECT bound_chat_jid, bound_group_folder, derived_thread_name "
            "FROM scheduled_tasks WHERE id = 'task-1'"
        )
        assert await cursor.fetchone() == (
            "slack:C123",
            "project__thread_slack-C123",
            "Build thread",
        )
