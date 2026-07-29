"""Legacy schema migration coverage through the public schema entry point."""

from __future__ import annotations

import aiosqlite
import pytest

from pynchy.state.schema import create_schema


@pytest.mark.asyncio
async def test_renamed_admin_values_are_copied_before_legacy_column_is_dropped() -> None:
    async with aiosqlite.connect(":memory:") as database:
        await database.execute(
            "CREATE TABLE registered_groups ("
            "jid TEXT PRIMARY KEY, name TEXT NOT NULL, folder TEXT NOT NULL UNIQUE, "
            "trigger_pattern TEXT NOT NULL, added_at TEXT NOT NULL, "
            "is_god INTEGER, is_admin INTEGER"
            ")"
        )
        await database.execute(
            "INSERT INTO registered_groups VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("slack:C123", "Project", "project", "@Pynchy", "now", 1, 0),
        )

        await create_schema(database)

        cursor = await database.execute(
            "SELECT is_admin FROM registered_groups WHERE jid = 'slack:C123'"
        )
        assert await cursor.fetchone() == (1,)
        cursor = await database.execute("PRAGMA table_info(registered_groups)")
        assert "is_god" not in {row[1] for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_repo_access_migration_copies_truthy_rows_and_drops_old_columns() -> None:
    async with aiosqlite.connect(":memory:") as database:
        await database.execute(
            "CREATE TABLE scheduled_tasks ("
            "id TEXT PRIMARY KEY, group_folder TEXT NOT NULL, chat_jid TEXT NOT NULL, "
            "prompt TEXT NOT NULL, schedule_type TEXT NOT NULL, schedule_value TEXT NOT NULL, "
            "next_run TEXT, status TEXT, created_at TEXT NOT NULL, "
            "pynchy_repo_access INTEGER, project_access TEXT"
            ")"
        )
        await database.execute(
            "INSERT INTO scheduled_tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "task-1",
                "project",
                "slack:C123",
                "check",
                "cron",
                "* * * * *",
                None,
                "active",
                "now",
                1,
                "old",
            ),
        )

        await create_schema(database)

        cursor = await database.execute(
            "SELECT repo_access FROM scheduled_tasks WHERE id = 'task-1'"
        )
        assert await cursor.fetchone() == ("pynchy",)
        cursor = await database.execute("PRAGMA table_info(scheduled_tasks)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert "pynchy_repo_access" not in columns
        assert "project_access" not in columns


@pytest.mark.asyncio
async def test_repo_access_migration_logs_when_legacy_columns_cannot_be_dropped() -> None:
    async with aiosqlite.connect(":memory:") as database:
        await database.execute(
            "CREATE TABLE scheduled_tasks ("
            "id TEXT PRIMARY KEY, group_folder TEXT NOT NULL, chat_jid TEXT NOT NULL, "
            "prompt TEXT NOT NULL, schedule_type TEXT NOT NULL, schedule_value TEXT NOT NULL, "
            "next_run TEXT, status TEXT, created_at TEXT NOT NULL, "
            "pynchy_repo_access INTEGER, project_access TEXT"
            ")"
        )
        await database.executescript(
            """
            CREATE TRIGGER keep_repo_access
            AFTER UPDATE OF pynchy_repo_access ON scheduled_tasks
            BEGIN
                SELECT NEW.pynchy_repo_access;
            END;
            CREATE TRIGGER keep_project_access
            AFTER UPDATE OF project_access ON scheduled_tasks
            BEGIN
                SELECT NEW.project_access;
            END;
            """
        )

        await create_schema(database)

        cursor = await database.execute("PRAGMA table_info(scheduled_tasks)")
        columns = {row[1] for row in await cursor.fetchall()}
        assert {"pynchy_repo_access", "project_access"} <= columns


@pytest.mark.asyncio
async def test_channel_cursor_seed_handles_invalid_then_valid_timestamp_maps() -> None:
    async with aiosqlite.connect(":memory:") as database:
        await create_schema(database)
        await database.execute(
            "INSERT INTO router_state (key, value) VALUES ('last_agent_timestamp', ?)",
            ("not-json",),
        )
        await create_schema(database)

        await database.execute(
            "UPDATE router_state SET value = ? WHERE key = 'last_agent_timestamp'",
            ('{"slack:C123": "2026-07-29T00:00:00Z"}',),
        )
        await database.execute(
            "INSERT INTO registered_groups "
            "(jid, name, folder, trigger_pattern, added_at) VALUES (?, ?, ?, ?, ?)",
            ("slack:C123", "Project", "project", "@Pynchy", "now"),
        )
        await create_schema(database)

        cursor = await database.execute(
            "SELECT channel_name, chat_jid, direction, cursor_value FROM channel_cursors"
        )
        assert await cursor.fetchall() == [
            ("slack", "slack:C123", "inbound", "2026-07-29T00:00:00Z")
        ]
