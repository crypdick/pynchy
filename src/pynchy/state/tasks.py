"""Scheduled task CRUD and run logging."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from aiosqlite import (
    Connection,
    Row,
)

from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
    TaskRunLog,
)
from pynchy.state.connection import _get_db, _update_by_id, atomic_write


def _row_to_task(row: Row) -> ScheduledTask:
    return ScheduledTask(
        id=row["id"],
        group_folder=row["group_folder"],
        chat_jid=row["chat_jid"],
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        session_policy=SessionPolicy(row["session_policy"] or SessionPolicy.RESET_BEFORE_RUN),
        next_run=row["next_run"],
        last_run=row["last_run"],
        last_result=row["last_result"],
        status=row["status"],
        created_at=row["created_at"],
        memory_enabled=bool(row["memory_enabled"]),
        repo_access=row["repo_access"] or None,
        input_source=row["input_source"] or "scheduled_task",
        config_job_name=row["config_job_name"] or None,
        config_job_is_deterministic=(
            None
            if row["config_job_is_deterministic"] is None
            else bool(row["config_job_is_deterministic"])
        ),
        config_job_command=row["config_job_command"] or None,
        config_job_cwd=row["config_job_cwd"] or None,
        config_job_timeout_seconds=row["config_job_timeout_seconds"],
        config_job_display_name=row["config_job_display_name"] or None,
        config_job_pre_run_command=row["config_job_pre_run_command"] or None,
        config_job_pre_run_cwd=row["config_job_pre_run_cwd"] or None,
        config_job_pre_run_timeout_seconds=row["config_job_pre_run_timeout_seconds"],
        derived_thread_name=row["derived_thread_name"] or None,
        bound_chat_jid=row["bound_chat_jid"] or None,
        bound_group_folder=row["bound_group_folder"] or None,
        conversation_id=row["conversation_id"] or None,
        last_reset_occurrence=row["last_reset_occurrence"] or None,
        occurrence_generation=row["occurrence_generation"],
        occurrence_due_at=row["occurrence_due_at"] or None,
        superseded_occurrence_generation=row["superseded_occurrence_generation"],
        superseded_occurrence_due_at=row["superseded_occurrence_due_at"] or None,
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
        temporal_workflow_run_id=row["temporal_workflow_run_id"],
        temporal_attempt=row["temporal_attempt"],
        turn_id=row["turn_id"],
        error_signature=row["error_signature"],
        escalation_reason=row["escalation_reason"],
    )


async def _insert_task(
    database: Connection, task: ScheduledTask, *, ignore_conflicts: bool = False
) -> bool:
    """Insert within the caller's transaction, including webhook receipt admission."""
    conflict_clause = "OR IGNORE" if ignore_conflicts else ""
    cursor = await database.execute(
        f"""
        INSERT {conflict_clause} INTO scheduled_tasks
            (id, group_folder, chat_jid, prompt, schedule_type,
             schedule_value, session_policy, next_run, status, created_at,
             memory_enabled, repo_access, input_source, config_job_name,
             config_job_is_deterministic,
             config_job_command, config_job_cwd, config_job_timeout_seconds,
             config_job_display_name, config_job_pre_run_command, config_job_pre_run_cwd,
             config_job_pre_run_timeout_seconds, derived_thread_name,
             bound_chat_jid, bound_group_folder, conversation_id, last_reset_occurrence,
             occurrence_generation, occurrence_due_at, superseded_occurrence_generation,
             superseded_occurrence_due_at)
        VALUES (:id, :group_folder, :chat_jid, :prompt, :schedule_type,
                :schedule_value, :session_policy, :next_run, :status, :created_at,
                :memory_enabled, :repo_access, :input_source, :config_job_name,
                :config_job_is_deterministic,
                :config_job_command, :config_job_cwd, :config_job_timeout_seconds,
                :config_job_display_name, :config_job_pre_run_command, :config_job_pre_run_cwd,
                :config_job_pre_run_timeout_seconds, :derived_thread_name,
                :bound_chat_jid, :bound_group_folder, :conversation_id, :last_reset_occurrence,
                :occurrence_generation, :occurrence_due_at, :superseded_occurrence_generation,
                :superseded_occurrence_due_at)
        """,  # noqa: S608 - conflict clause uses fixed literals; task values are bound parameters.
        asdict(task),
    )
    return cursor.rowcount == 1


async def create_task(task: ScheduledTask) -> None:
    """Create a task with next_run unset; Temporal owns its schedule."""
    async with atomic_write() as db:
        await _insert_task(db, replace(task, next_run=None, repo_access=task.repo_access or None))


async def create_task_if_absent(task: ScheduledTask) -> bool:
    """Atomically create an externally discovered task once by stable ID."""
    async with atomic_write() as db:
        return await _insert_task(db, task, ignore_conflicts=True)


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


async def get_tasks_for_conversation(conversation_id: str) -> list[ScheduledTask]:
    """Return scheduled work still owned by one routed conversation."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM scheduled_tasks
        WHERE conversation_id = ? AND status IN ('active', 'paused')
        ORDER BY created_at DESC
        """,
        (conversation_id,),
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
    "session_policy",
    "status",
    "memory_enabled",
    "repo_access",
    "input_source",
    "config_job_name",
    "config_job_is_deterministic",
    "config_job_command",
    "config_job_cwd",
    "config_job_timeout_seconds",
    "config_job_display_name",
    "config_job_pre_run_command",
    "config_job_pre_run_cwd",
    "config_job_pre_run_timeout_seconds",
    "derived_thread_name",
    "bound_chat_jid",
    "bound_group_folder",
    "conversation_id",
    "last_reset_occurrence",
}


async def update_task(task_id: str, updates: dict[str, Any]) -> None:
    """Update specific fields of a task."""
    await _update_by_id("scheduled_tasks", task_id, updates, _TASK_UPDATE_FIELDS)


async def cancel_task_and_checkpoint(task_id: str) -> None:
    """Cancel scheduled work and retire any resumable occurrence atomically."""
    async with atomic_write() as db:
        await db.execute(
            """
            UPDATE scheduled_tasks
            SET status = 'cancelled', next_run = NULL
            WHERE id = ?
            """,
            (task_id,),
        )
        await db.execute(
            "DELETE FROM in_flight_turns WHERE task_id = ?",
            (task_id,),
        )


async def resume_task(task_id: str) -> None:
    """Reactivate a task and begin one fresh circuit-breaker failure window."""
    await _resume_paused_task(
        task_id,
        require_no_in_flight_turn=False,
        retire_unclaimed_scheduled_turn=False,
    )


async def resume_task_if_no_in_flight_turn(task_id: str) -> bool:
    """Reactivate a paused task only when no scheduled turn owns it."""
    return await _resume_paused_task(
        task_id,
        require_no_in_flight_turn=True,
        retire_unclaimed_scheduled_turn=False,
    )


async def resume_once_task_after_unclaimed_scheduled_turn(task_id: str) -> bool:
    """Resume a paused one-shot task after atomically retiring its terminal checkpoint."""
    return await _resume_paused_task(
        task_id,
        require_no_in_flight_turn=True,
        retire_unclaimed_scheduled_turn=True,
    )


async def _resume_paused_task(
    task_id: str,
    *,
    require_no_in_flight_turn: bool,
    retire_unclaimed_scheduled_turn: bool = False,
) -> bool:
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        if retire_unclaimed_scheduled_turn:
            await db.execute(
                """
                DELETE FROM in_flight_turns
                WHERE task_id = ? AND work_kind = 'scheduled' AND claimed_at IS NULL
                  AND control_state = 'active'
                  AND EXISTS (
                      SELECT 1 FROM scheduled_tasks
                      WHERE id = ? AND status = 'paused' AND schedule_type = 'once'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM in_flight_turns AS retained
                      WHERE retained.task_id = ?
                        AND (
                            retained.work_kind != 'scheduled'
                            OR retained.claimed_at IS NOT NULL
                            OR retained.control_state != 'active'
                        )
                  )
                """,
                (task_id, task_id, task_id),
            )
        cursor = await db.execute(
            """
            UPDATE scheduled_tasks
            SET status = 'active',
                superseded_occurrence_due_at = CASE
                    WHEN schedule_type = 'once'
                    THEN COALESCE(occurrence_due_at, schedule_value)
                    ELSE superseded_occurrence_due_at
                END,
                superseded_occurrence_generation = CASE
                    WHEN schedule_type = 'once' THEN occurrence_generation
                    ELSE superseded_occurrence_generation
                END,
                occurrence_due_at = CASE
                    WHEN schedule_type = 'once' THEN ?
                    ELSE occurrence_due_at
                END,
                occurrence_generation = occurrence_generation
                    + CASE WHEN schedule_type = 'once' THEN 1 ELSE 0 END
            WHERE id = ? AND status = 'paused'
              AND (
                  ? = 0
                  OR NOT EXISTS (SELECT 1 FROM in_flight_turns WHERE task_id = ?)
              )
            """,
            (now, task_id, int(require_no_in_flight_turn), task_id),
        )
        if cursor.rowcount != 1:
            return False
        await db.execute(
            """
            INSERT INTO task_run_logs (
                task_id, run_at, duration_ms, status, result, error,
                temporal_workflow_id, temporal_workflow_run_id, temporal_attempt,
                turn_id, error_signature, escalation_reason
            )
            VALUES (?, ?, 0, 'resumed', 'Task resumed', NULL, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (task_id, now),
        )
    return True


async def rebind_task_root(task_id: str, *, group_folder: str, chat_jid: str) -> None:
    """Move a config task to a replacement root workspace."""
    async with atomic_write() as db:
        await db.execute(
            """
            UPDATE scheduled_tasks
            SET group_folder = ?, chat_jid = ?
            WHERE id = ?
            """,
            (group_folder, chat_jid, task_id),
        )


async def delete_task(task_id: str) -> None:
    """Delete a task, its run logs, and any unfinished agent checkpoint."""
    async with atomic_write() as db:
        await db.execute("DELETE FROM in_flight_turns WHERE task_id = ?", (task_id,))
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
    now = datetime.now(UTC).isoformat()
    async with atomic_write() as db:
        await db.execute(
            """
            UPDATE scheduled_tasks
            SET last_run = ?, last_result = ?,
                status = CASE WHEN ? THEN 'completed' ELSE status END
            WHERE id = ?
            """,
            (now, last_result, completed, task_id),
        )


async def log_task_run(log: TaskRunLog) -> None:
    """Log a task run."""
    async with atomic_write() as db:
        await db.execute(
            """
            INSERT INTO task_run_logs (
                task_id, run_at, duration_ms, status, result, error,
                temporal_workflow_id, temporal_workflow_run_id, temporal_attempt,
                turn_id, error_signature, escalation_reason
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log.task_id,
                log.run_at,
                log.duration_ms,
                log.status,
                log.result,
                log.error,
                log.temporal_workflow_id,
                log.temporal_workflow_run_id,
                log.temporal_attempt,
                log.turn_id,
                log.error_signature,
                log.escalation_reason,
            ),
        )


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
