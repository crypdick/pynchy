"""Schema migrations owned by scheduled-task persistence."""

from __future__ import annotations

import aiosqlite

from pynchy.logger import logger


async def clear_temporal_owned_next_runs(database: aiosqlite.Connection) -> None:
    """Clear local timing values from Temporal-owned scheduled-work rows."""
    await database.execute("UPDATE scheduled_tasks SET next_run = NULL WHERE next_run IS NOT NULL")
    await database.execute("UPDATE host_jobs SET next_run = NULL WHERE next_run IS NOT NULL")
    await database.commit()


async def drop_derived_task_thread_columns(database: aiosqlite.Connection) -> None:
    """Remove task-thread cache columns superseded by per-run derivation."""
    cursor = await database.execute("PRAGMA table_info(scheduled_tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    for column in ("persistent_thread_name", "persistent_thread_jid"):
        if column not in cols:
            continue
        try:
            await database.execute(f"ALTER TABLE scheduled_tasks DROP COLUMN {column}")
            logger.info("Dropped derived scheduled-task thread column", column=column)
        except aiosqlite.OperationalError as exc:
            logger.warning(
                "Failed to drop derived scheduled-task thread column",
                column=column,
                err=str(exc),
            )
    await database.commit()
