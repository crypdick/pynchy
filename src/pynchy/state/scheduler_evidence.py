"""Bounded durable evidence for scheduled work."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from croniter import croniter

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.scheduling.api import (
    SchedulerAuditClassification,
    SchedulerAuditSlot,
    SchedulerDefinition,
    SchedulerEvidenceOutcome,
    SchedulerOccurrence,
)
from pynchy.state.connection import _get_db, atomic_write


def scheduler_definition_hash(
    schedule_key: str,
    schedule_type: str,
    schedule_value: str,
    timezone: str,
) -> str:
    """Return a stable revision ID for a schedule specification."""
    material = "\x1f".join((schedule_key, schedule_type, schedule_value, timezone))
    return hashlib.sha256(material.encode()).hexdigest()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _row_to_definition(row: Row) -> SchedulerDefinition:
    return SchedulerDefinition(
        schedule_key=row["schedule_key"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        timezone=row["timezone"],
        active_from=row["active_from"],
        active_to=row["active_to"],
        definition_hash=row["definition_hash"],
    )


def _row_to_occurrence(row: Row) -> SchedulerOccurrence:
    return SchedulerOccurrence(
        definition_hash=row["definition_hash"],
        scheduled_at=row["scheduled_at"],
        outcome=SchedulerEvidenceOutcome(row["outcome"]),
        dispatched_at=row["dispatched_at"],
        terminal_at=row["terminal_at"],
        reason=row["reason"],
        workflow_id=row["workflow_id"],
        run_id=row["run_id"],
        attempts=row["attempts"],
    )


async def register_scheduler_definition(definition: SchedulerDefinition) -> SchedulerDefinition:
    """Persist a definition revision and close any active revision with a different hash."""
    async with atomic_write() as db:
        await db.execute(
            """
            UPDATE scheduler_definitions
            SET active_to = ?
            WHERE schedule_key = ? AND active_to IS NULL AND definition_hash != ?
            """,
            (definition.active_from, definition.schedule_key, definition.definition_hash),
        )
        await db.execute(
            """
            INSERT OR IGNORE INTO scheduler_definitions (
                definition_hash, schedule_key, schedule_type, schedule_value, timezone,
                active_from, active_to, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                definition.definition_hash,
                definition.schedule_key,
                definition.schedule_type,
                definition.schedule_value,
                definition.timezone,
                definition.active_from,
                definition.active_to,
                definition.active_from,
            ),
        )
    return definition


async def record_scheduler_occurrence(occurrence: SchedulerOccurrence) -> SchedulerOccurrence:
    """Create or advance one logical slot without duplicating retry evidence."""
    async with atomic_write() as db:
        await db.execute(
            """
            INSERT INTO scheduler_occurrences (
                definition_hash, scheduled_at, outcome, dispatched_at, terminal_at,
                reason, workflow_id, run_id, attempts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(definition_hash, scheduled_at) DO UPDATE SET
                outcome = excluded.outcome,
                dispatched_at = COALESCE(
                    scheduler_occurrences.dispatched_at, excluded.dispatched_at
                ),
                terminal_at = COALESCE(excluded.terminal_at, scheduler_occurrences.terminal_at),
                reason = COALESCE(excluded.reason, scheduler_occurrences.reason),
                workflow_id = COALESCE(excluded.workflow_id, scheduler_occurrences.workflow_id),
                run_id = COALESCE(excluded.run_id, scheduler_occurrences.run_id),
                attempts = MAX(scheduler_occurrences.attempts, excluded.attempts)
            """,
            (
                occurrence.definition_hash,
                occurrence.scheduled_at,
                occurrence.outcome.value,
                occurrence.dispatched_at,
                occurrence.terminal_at,
                occurrence.reason,
                occurrence.workflow_id,
                occurrence.run_id,
                occurrence.attempts,
            ),
        )
    return occurrence


async def get_scheduler_occurrences(
    definition_hash: str, *, start_at: str, end_at: str
) -> list[SchedulerOccurrence]:
    """Return retained evidence for one bounded audit range."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM scheduler_occurrences
        WHERE definition_hash = ? AND scheduled_at >= ? AND scheduled_at <= ?
        ORDER BY scheduled_at
        """,
        (definition_hash, start_at, end_at),
    )
    return [_row_to_occurrence(row) for row in await cursor.fetchall()]


async def prune_scheduler_evidence(*, before: str) -> int:
    """Prune terminal rows and atomically preserve an audit coverage watermark."""
    terminal = (
        SchedulerEvidenceOutcome.SUCCEEDED.value,
        SchedulerEvidenceOutcome.FAILED.value,
        SchedulerEvidenceOutcome.GATE_SKIPPED.value,
        SchedulerEvidenceOutcome.POLICY_SKIPPED.value,
        SchedulerEvidenceOutcome.MISSED.value,
    )
    async with atomic_write() as db:
        cursor = await db.execute(
            """
            SELECT definition_hash, MAX(scheduled_at) AS watermark
            FROM scheduler_occurrences
            WHERE scheduled_at < ? AND outcome IN (?, ?, ?, ?, ?)
            GROUP BY definition_hash
            """,
            (before, *terminal),
        )
        rows = await cursor.fetchall()
        for row in rows:
            await db.execute(
                """
                UPDATE scheduler_definitions
                SET retention_watermark = MAX(COALESCE(retention_watermark, ''), ?)
                WHERE definition_hash = ?
                """,
                (row["watermark"], row["definition_hash"]),
            )
        cursor = await db.execute(
            """
            DELETE FROM scheduler_occurrences
            WHERE scheduled_at < ? AND outcome IN (?, ?, ?, ?, ?)
            """,
            (before, *terminal),
        )
    return cursor.rowcount


def _expected_slots(definition: SchedulerDefinition, start: datetime, end: datetime) -> list[str]:
    active_start = max(start, _parse_timestamp(definition.active_from))
    active_end = min(end, _parse_timestamp(definition.active_to)) if definition.active_to else end
    if active_start > active_end:
        return []
    if definition.schedule_type == "once":
        due_at = _parse_timestamp(definition.schedule_value)
        return [_timestamp(due_at)] if active_start <= due_at <= active_end else []
    if definition.schedule_type == "interval":
        interval = timedelta(minutes=int(definition.schedule_value))
        slots: list[str] = []
        current = _parse_timestamp(definition.active_from)
        while current < active_start:
            current += interval
        while current <= active_end:
            slots.append(_timestamp(current))
            current += interval
        return slots
    iterator = croniter(definition.schedule_value, active_start - timedelta(seconds=1))
    slots = []
    while (current := iterator.get_next(datetime)).astimezone(UTC) <= active_end:
        slots.append(_timestamp(current))
    return slots


async def audit_scheduler_evidence(
    *, start_at: str, end_at: str, now: str, missed_after_seconds: int
) -> list[SchedulerAuditSlot]:
    """Classify every expected slot without treating absent evidence as a pass."""
    start = _parse_timestamp(start_at)
    end = _parse_timestamp(end_at)
    now_at = _parse_timestamp(now)
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT * FROM scheduler_definitions
        WHERE active_from <= ? AND (active_to IS NULL OR active_to >= ?)
        ORDER BY schedule_key, active_from
        """,
        (end_at, start_at),
    )
    definitions = [
        (_row_to_definition(row), row["retention_watermark"]) for row in await cursor.fetchall()
    ]
    slots: list[SchedulerAuditSlot] = []
    for definition, watermark in definitions:
        occurrences = await get_scheduler_occurrences(
            definition.definition_hash, start_at=start_at, end_at=end_at
        )
        evidence = {item.scheduled_at: item for item in occurrences}
        for scheduled_at in _expected_slots(definition, start, end):
            occurrence = evidence.get(scheduled_at)
            if occurrence is not None:
                classification = {
                    SchedulerEvidenceOutcome.SUCCEEDED: SchedulerAuditClassification.RUN,
                    SchedulerEvidenceOutcome.GATE_SKIPPED: (
                        SchedulerAuditClassification.DECLARED_SKIP
                    ),
                    SchedulerEvidenceOutcome.POLICY_SKIPPED: (
                        SchedulerAuditClassification.DECLARED_SKIP
                    ),
                    SchedulerEvidenceOutcome.FAILED: SchedulerAuditClassification.FAILED,
                    SchedulerEvidenceOutcome.MISSED: SchedulerAuditClassification.MISSED,
                    SchedulerEvidenceOutcome.PENDING: SchedulerAuditClassification.PENDING,
                }[occurrence.outcome]
                slots.append(
                    SchedulerAuditSlot(
                        definition.schedule_key,
                        scheduled_at,
                        classification,
                        occurrence.outcome,
                        occurrence.reason,
                    )
                )
            elif watermark is not None and scheduled_at <= watermark:
                slots.append(
                    SchedulerAuditSlot(
                        definition.schedule_key,
                        scheduled_at,
                        SchedulerAuditClassification.RETENTION_GAP,
                    )
                )
            elif _parse_timestamp(scheduled_at) + timedelta(seconds=missed_after_seconds) < now_at:
                slots.append(
                    SchedulerAuditSlot(
                        definition.schedule_key,
                        scheduled_at,
                        SchedulerAuditClassification.MISSED,
                    )
                )
            else:
                slots.append(
                    SchedulerAuditSlot(
                        definition.schedule_key,
                        scheduled_at,
                        SchedulerAuditClassification.PENDING,
                    )
                )
    return slots
