"""Schema migrations owned by scheduled-task persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiosqlite import OperationalError

if TYPE_CHECKING:
    import aiosqlite

from pynchy.config.workspace_names import dynamic_thread_folder
from pynchy.logger import logger


async def clear_temporal_owned_next_runs(database: aiosqlite.Connection) -> None:
    """Clear local timing values from Temporal-owned scheduled-work rows."""
    await database.execute("UPDATE scheduled_tasks SET next_run = NULL WHERE next_run IS NOT NULL")
    await database.execute("UPDATE host_jobs SET next_run = NULL WHERE next_run IS NOT NULL")
    await database.commit()


async def migrate_scheduled_session_policy(database: aiosqlite.Connection) -> None:
    """Map tool-facing context modes to durable session policy."""
    cursor = await database.execute("PRAGMA table_info(scheduled_tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "context_mode" not in cols or "session_policy" not in cols:
        return
    await database.execute(
        """
        UPDATE scheduled_tasks
        SET session_policy = CASE context_mode
            WHEN 'group' THEN 'continue'
            ELSE 'reset_before_run'
        END
        """
    )
    await database.execute("ALTER TABLE scheduled_tasks DROP COLUMN context_mode")
    logger.info("Dropped scheduled_tasks.context_mode column")
    await database.commit()


async def migrate_cached_task_thread_binding(database: aiosqlite.Connection) -> None:
    """Promote cached config-thread destinations into durable runtime ownership."""
    cursor = await database.execute("PRAGMA table_info(scheduled_tasks)")
    cols = {row[1] for row in await cursor.fetchall()}
    if "persistent_thread_jid" in cols:
        cursor = await database.execute(
            """
            SELECT id, group_folder, persistent_thread_jid
            FROM scheduled_tasks
            WHERE persistent_thread_jid IS NOT NULL
            """
        )
        for task_id, group_folder, thread_jid in await cursor.fetchall():
            await database.execute(
                """
                UPDATE scheduled_tasks
                SET bound_chat_jid = COALESCE(bound_chat_jid, ?),
                    bound_group_folder = COALESCE(bound_group_folder, ?)
                WHERE id = ?
                """,
                (
                    thread_jid,
                    dynamic_thread_folder(group_folder, thread_jid),
                    task_id,
                ),
            )
    if "persistent_thread_name" in cols:
        await database.execute(
            """
            UPDATE scheduled_tasks
            SET derived_thread_name = COALESCE(
                derived_thread_name,
                persistent_thread_name
            )
            """
        )
    for column in ("persistent_thread_name", "persistent_thread_jid"):
        if column not in cols:
            continue
        try:
            await database.execute(f"ALTER TABLE scheduled_tasks DROP COLUMN {column}")
            logger.info("Dropped legacy scheduled-task thread column", column=column)
        except OperationalError as exc:
            logger.warning(
                "Failed to drop legacy scheduled-task thread column",
                column=column,
                err=str(exc),
            )
    await database.commit()
