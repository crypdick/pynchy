"""Scheduled task CRUD and run logging."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.state.connection import _get_db, _update_by_id, atomic_write
from pynchy.types import ScheduledTask, TaskRunLog


def _row_to_task(row: Row) -> ScheduledTask:
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
        repo_access=row["repo_access"] or None,
        input_source=row["input_source"] or "scheduled_task",
        config_job_name=row["config_job_name"] or None,
        derived_thread_name=row["derived_thread_name"] or None,
    )


def _row_to_task_run_log(row: Row) -> TaskRunLog:
    return TaskRunLog(
        task_id=row["task_id"],
        run_at=row["run_at"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        result=row["result"],
        error=row["error"],
        temporal_workflow_id=row["temporal_workflow_id"],
        temporal_attempt=row["temporal_attempt"],
        error_signature=row["error_signature"],
        escalation_reason=row["escalation_reason"],
    )


async def create_task(task: ScheduledTask) -> None:
    """Create a scheduled task."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO scheduled_tasks
            (id, group_folder, chat_jid, prompt, schedule_type,
             schedule_value, context_mode, next_run, status, created_at,
             repo_access, input_source, config_job_name, derived_thread_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode,
            None,
            task.status,
            task.created_at,
            task.repo_access or None,
            task.input_source,
            task.config_job_name,
            task.derived_thread_name,
        ),
    )
    await db.commit()


async def create_task_if_absent(task: ScheduledTask) -> bool:
    """Atomically create an externally discovered task once by stable ID."""
    db = _get_db()
    cursor = await db.execute(
        """
        INSERT OR IGNORE INTO scheduled_tasks
            (id, group_folder, chat_jid, prompt, schedule_type,
             schedule_value, context_mode, next_run, status, created_at,
             repo_access, input_source, config_job_name, derived_thread_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task.id,
            task.group_folder,
            task.chat_jid,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode,
            task.next_run,
            task.status,
            task.created_at,
            task.repo_access,
            task.input_source,
            task.config_job_name,
            task.derived_thread_name,
        ),
    )
    await db.commit()
    return cursor.rowcount == 1


async def get_task_by_id(task_id: str) -> ScheduledTask | None:
    """Get a task by its ID."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_task(row)


async def get_tasks_for_group(group_folder: str) -> list[ScheduledTask]:
    """Get all tasks for a group, ordered by creation date."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM scheduled_tasks WHERE group_folder = ? ORDER BY created_at DESC",
        (group_folder,),
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def get_all_tasks() -> list[ScheduledTask]:
    """Get all tasks, ordered by creation date."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


_TASK_UPDATE_FIELDS = {
    "chat_jid",
    "prompt",
    "schedule_type",
    "schedule_value",
    "context_mode",
    "status",
    "repo_access",
    "input_source",
    "config_job_name",
    "derived_thread_name",
}


async def update_task(task_id: str, updates: dict[str, Any]) -> None:
    """Update specific fields of a task."""
    await _update_by_id("scheduled_tasks", task_id, updates, _TASK_UPDATE_FIELDS)


async def rebind_task_root(task_id: str, *, group_folder: str, chat_jid: str) -> None:
    """Move a config task to a replacement root workspace."""
    db = _get_db()
    await db.execute(
        """
        UPDATE scheduled_tasks
        SET group_folder = ?, chat_jid = ?
        WHERE id = ?
        """,
        (group_folder, chat_jid, task_id),
    )
    await db.commit()


async def delete_task(task_id: str) -> None:
    """Delete a task and its run logs."""
    async with atomic_write() as db:
        await db.execute("DELETE FROM task_run_logs WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))


async def get_active_task_for_group(group_folder: str) -> ScheduledTask | None:
    """Find the active scheduled task for a periodic agent group."""
    db = _get_db()
    cursor = await db.execute(
        "SELECT * FROM scheduled_tasks WHERE group_folder = ? AND status = 'active' LIMIT 1",
        (group_folder,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_task(row)


async def record_task_completion(task_id: str, *, last_result: str, completed: bool) -> None:
    """Record task execution evidence without maintaining Temporal-owned timing."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        UPDATE scheduled_tasks
        SET last_run = ?, last_result = ?,
            status = CASE WHEN ? THEN 'completed' ELSE status END
        WHERE id = ?
        """,
        (now, last_result, completed, task_id),
    )
    await db.commit()


async def log_task_run(log: TaskRunLog) -> None:
    """Log a task run."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO task_run_logs (
            task_id, run_at, duration_ms, status, result, error,
            temporal_workflow_id, temporal_attempt, error_signature, escalation_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            log.task_id,
            log.run_at,
            log.duration_ms,
            log.status,
            log.result,
            log.error,
            log.temporal_workflow_id,
            log.temporal_attempt,
            log.error_signature,
            log.escalation_reason,
        ),
    )
    await db.commit()


async def get_task_run_logs(task_id: str, *, limit: int = 20) -> list[TaskRunLog]:
    """Return recent run logs for a scheduled task, newest first."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM task_run_logs
        WHERE task_id = ?
        ORDER BY run_at DESC, id DESC
        LIMIT ?
        """,
        (task_id, limit),
    )
    rows = await cursor.fetchall()
    return [_row_to_task_run_log(row) for row in rows]
