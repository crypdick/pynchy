"""Host job CRUD — shell commands scheduled on the host (no LLM/container)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pynchy.state.connection import _get_db, _update_by_id
from pynchy.types import HostJob


def _row_to_host_job(row) -> HostJob:
    return HostJob(
        id=row["id"],
        name=row["name"],
        command=row["command"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        created_by=row["created_by"],
        next_run=row["next_run"],
        last_run=row["last_run"],
        status=row["status"],
        created_at=row["created_at"],
        cwd=row["cwd"],
        timeout_seconds=row["timeout_seconds"],
        enabled=bool(row["enabled"]),
    )


async def create_host_job(job: dict[str, Any]) -> None:
    """Create a new host job."""
    db = _get_db()
    await db.execute(
        """
        INSERT INTO host_jobs
            (id, name, command, schedule_type, schedule_value,
             next_run, status, created_at, created_by, cwd,
             timeout_seconds, enabled)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job["id"],
            job["name"],
            job["command"],
            job["schedule_type"],
            job["schedule_value"],
            job.get("next_run"),
            job["status"],
            job["created_at"],
            job["created_by"],
            job.get("cwd"),
            job.get("timeout_seconds", 600),
            1 if job.get("enabled", True) else 0,
        ),
    )
    await db.commit()


async def get_due_host_jobs() -> list[HostJob]:
    """Get all active and enabled host jobs that are due to run."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    cursor = await db.execute(
        """
        SELECT * FROM host_jobs
        WHERE status = 'active' AND enabled = 1
              AND next_run IS NOT NULL AND next_run <= ?
        ORDER BY next_run
        """,
        (now,),
    )
    rows = await cursor.fetchall()
    return [_row_to_host_job(row) for row in rows]


async def update_host_job_after_run(job_id: str, next_run: str | None, exit_code: int) -> None:
    """Update a host job after it has been run."""
    db = _get_db()
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """
        UPDATE host_jobs
        SET next_run = ?, last_run = ?,
            status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
        WHERE id = ?
        """,
        (next_run, now, next_run, job_id),
    )
    await db.commit()


async def get_host_job_by_id(job_id: str) -> HostJob | None:
    """Get a host job by its ID."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM host_jobs WHERE id = ?", (job_id,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_host_job(row)


async def get_host_job_by_name(name: str) -> HostJob | None:
    """Get a host job by its unique name."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM host_jobs WHERE name = ?", (name,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_host_job(row)


async def get_all_host_jobs() -> list[HostJob]:
    """Get all host jobs, ordered by creation date."""
    db = _get_db()
    cursor = await db.execute("SELECT * FROM host_jobs ORDER BY created_at DESC")
    rows = await cursor.fetchall()
    return [_row_to_host_job(row) for row in rows]


_HOST_JOB_UPDATE_FIELDS = {"status", "enabled", "next_run", "schedule_value"}


async def update_host_job(job_id: str, updates: dict[str, Any]) -> None:
    """Update specific fields of a host job."""
    await _update_by_id("host_jobs", job_id, updates, _HOST_JOB_UPDATE_FIELDS)


async def delete_host_job(job_id: str) -> None:
    """Delete a host job."""
    db = _get_db()
    await db.execute("DELETE FROM host_jobs WHERE id = ?", (job_id,))
    await db.commit()
