"""Scheduled task CRUD and run logging."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pynchy.db._connection import _get_db
from pynchy.types import ScheduledTask, TaskRunLog


def _row_to_task(row) -> ScheduledTask:
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
        project_access=bool(row["project_access"]),
    )


async def create_task(task: dict[str, Any]) -> None:
    """Create a new scheduled task."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO scheduled_tasks
            (id, group_folder, chat_jid, prompt, schedule_type,
             schedule_value, context_mode, next_run, status, created_at,
             project_access)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task["id"],
            task["group_folder"],
            task["chat_jid"],
            task["prompt"],
            task["schedule_type"],
            task["schedule_value"],
            task.get("context_mode", "isolated"),
            task.get("next_run"),
            task["status"],
            task["created_at"],
            1 if task.get("project_access") else 0,
        ),
    )
    await db.commit()


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


async def update_task(task_id: str, updates: dict[str, Any]) -> None:
    """Update specific fields of a task."""
    allowed = {"prompt", "schedule_type", "schedule_value", "next_run", "status", "project_access"}
    fields: list[str] = []
    values: list[Any] = []

    for key, value in updates.items():
        if key in allowed:
            fields.append(f"{key} = ?")
            values.append(value)

    if not fields:
        return

    values.append(task_id)
    db = _get_db()
    await db.execute(
        f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?",
        values,
    )
    await db.commit()


async def delete_task(task_id: str) -> None:
    """Delete a task and its run logs."""
    db = _get_db()
    await db.execute("DELETE FROM task_run_logs WHERE task_id = ?", (task_id,))
    await db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    await db.commit()


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


async def get_due_tasks() -> list[ScheduledTask]:
    """Get all active tasks that are due to run."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
        ORDER BY next_run
        """,
        (now,),
    )
    rows = await cursor.fetchall()
    return [_row_to_task(row) for row in rows]


async def update_task_after_run(task_id: str, next_run: str | None, last_result: str) -> None:
    """Update a task after it has been run."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        UPDATE scheduled_tasks
        SET next_run = ?, last_run = ?, last_result = ?,
            status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
        WHERE id = ?
        """,
        (next_run, now, last_result, next_run, task_id),
    )
    await db.commit()


async def log_task_run(log: TaskRunLog) -> None:
    """Log a task run."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (log.task_id, log.run_at, log.duration_ms, log.status, log.result, log.error),
    )
    await db.commit()
