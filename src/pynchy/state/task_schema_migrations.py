"""Schema migrations owned by scheduled-task persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiosqlite import OperationalError

if TYPE_CHECKING:
    import aiosqlite

from pynchy.conversation.workspaces import dynamic_thread_folder
from pynchy.logger import logger

TASK_SCHEMA = """\
CREATE TABLE IF NOT EXISTS scheduled_tasks (
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
    config_job_is_deterministic INTEGER,
    config_job_command TEXT,
    config_job_cwd TEXT,
    config_job_timeout_seconds INTEGER,
    config_job_display_name TEXT,
    config_job_pre_run_command TEXT,
    config_job_pre_run_cwd TEXT,
    config_job_pre_run_timeout_seconds INTEGER,
    derived_thread_name TEXT,
    bound_chat_jid TEXT,
    bound_group_folder TEXT,
    conversation_id TEXT,
    last_reset_occurrence TEXT,
    occurrence_generation INTEGER NOT NULL DEFAULT 0,
    occurrence_due_at TEXT,
    superseded_occurrence_generation INTEGER,
    superseded_occurrence_due_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);
CREATE INDEX IF NOT EXISTS idx_group_folder ON scheduled_tasks(group_folder);

CREATE TABLE IF NOT EXISTS task_run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    temporal_workflow_id TEXT,
    temporal_workflow_run_id TEXT,
    temporal_attempt INTEGER,
    turn_id TEXT,
    error_signature TEXT,
    escalation_reason TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

"""


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
