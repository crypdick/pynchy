"""Scheduler-evidence recording at Temporal activity boundaries."""

from __future__ import annotations

from datetime import UTC, datetime

from pynchy.scheduling.api import (
    HostJob,
    ScheduledTask,
    SchedulerDefinition,
    SchedulerEvidenceOutcome,
    SchedulerOccurrence,
)
from pynchy.state.api import (
    record_scheduler_occurrence,
    register_scheduler_definition,
    scheduler_definition_hash,
)


async def record_task_occurrence(
    task: ScheduledTask,
    *,
    scheduled_at: str,
    outcome: SchedulerEvidenceOutcome,
    workflow_id: str | None,
    reason: str | None = None,
) -> None:
    """Persist one agent-task occurrence against its active schedule revision."""
    await _record_occurrence(
        schedule_key=f"agent-task:{task.id}",
        schedule_type=task.schedule_type,
        schedule_value=task.schedule_value,
        scheduled_at=scheduled_at,
        outcome=outcome,
        workflow_id=workflow_id,
        reason=reason,
    )


async def record_host_job_occurrence(
    job: HostJob,
    *,
    scheduled_at: str,
    outcome: SchedulerEvidenceOutcome,
    workflow_id: str | None,
    reason: str | None = None,
) -> None:
    """Persist one database-host-job occurrence against its active revision."""
    await _record_occurrence(
        schedule_key=f"host-job:{job.id}",
        schedule_type=job.schedule_type,
        schedule_value=job.schedule_value,
        scheduled_at=scheduled_at,
        outcome=outcome,
        workflow_id=workflow_id,
        reason=reason,
    )


async def record_config_host_cron_occurrence(  # noqa: PLR0913 - one occurrence needs its identity and terminal evidence.
    job_name: str,
    *,
    schedule_value: str,
    scheduled_at: str,
    outcome: SchedulerEvidenceOutcome,
    workflow_id: str | None,
    reason: str | None = None,
) -> None:
    """Persist one configuration-owned host cron occurrence."""
    await _record_occurrence(
        schedule_key=f"host-cron:{job_name}",
        schedule_type="cron",
        schedule_value=schedule_value,
        scheduled_at=scheduled_at,
        outcome=outcome,
        workflow_id=workflow_id,
        reason=reason,
    )


async def _record_occurrence(  # noqa: PLR0913 - persistence needs the complete occurrence identity.
    *,
    schedule_key: str,
    schedule_type: str,
    schedule_value: str,
    scheduled_at: str,
    outcome: SchedulerEvidenceOutcome,
    workflow_id: str | None,
    reason: str | None,
) -> None:
    timezone = "UTC"
    definition_hash = scheduler_definition_hash(
        schedule_key, schedule_type, schedule_value, timezone
    )
    await register_scheduler_definition(
        SchedulerDefinition(
            schedule_key=schedule_key,
            schedule_type=schedule_type,  # type: ignore[arg-type]
            schedule_value=schedule_value,
            timezone=timezone,
            active_from=scheduled_at,
            definition_hash=definition_hash,
        )
    )
    terminal_at = (
        None if outcome is SchedulerEvidenceOutcome.PENDING else datetime.now(UTC).isoformat()
    )
    await record_scheduler_occurrence(
        SchedulerOccurrence(
            definition_hash=definition_hash,
            scheduled_at=scheduled_at,
            outcome=outcome,
            dispatched_at=scheduled_at,
            terminal_at=terminal_at,
            reason=reason,
            workflow_id=workflow_id,
            attempts=1,
        )
    )
