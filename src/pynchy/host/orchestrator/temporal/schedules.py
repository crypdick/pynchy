"""Temporal Schedule construction for Pynchy scheduled work."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)

from pynchy.config import get_settings
from pynchy.host.orchestrator.temporal.workflows import (
    ChannelReconciliationWorkflow,
    ConfigHostCronWorkflow,
    DatabaseHostJobWorkflow,
    ExternalGitSyncWorkflow,
    HostGitSyncWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.types import (  # noqa: TC001, RUF100 - beartype resolves Temporal schedule annotations at runtime.
    HostJob,
    ScheduledTask,
)

TEMPORAL_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
SCHEDULE_PREFIXES = (
    "pynchy-agent-schedule-",
    "pynchy-host-job-schedule-",
    "pynchy-host-cron-schedule-",
    "pynchy-git-sync-",
    "pynchy-channel-reconciliation",
)
HOST_GIT_SYNC_SCHEDULE_ID = "pynchy-git-sync-host"
CHANNEL_RECONCILIATION_SCHEDULE_ID = "pynchy-channel-reconciliation"
HOST_GIT_SYNC_INTERVAL_SECONDS = 5
CHANNEL_RECONCILIATION_INTERVAL_SECONDS = 10


def safe_workflow_fragment(value: str) -> str:
    """Return a value safe for Temporal workflow and schedule IDs."""
    return TEMPORAL_SAFE.sub("-", value).strip("-").replace("+", "-")


def agent_task_workflow_id(task: ScheduledTask) -> str:
    """Return the idempotency key for a one-time scheduled agent task."""
    due_at = task.next_run or "unscheduled"
    return f"pynchy-agent-task-{safe_workflow_fragment(task.id)}-{safe_workflow_fragment(due_at)}"


def agent_task_schedule_id(task: ScheduledTask) -> str:
    """Return the Temporal Schedule ID for a recurring agent task."""
    return f"pynchy-agent-schedule-{safe_workflow_fragment(task.id)}"


def database_host_job_workflow_id(job: HostJob) -> str:
    """Return the idempotency key for a one-time database host job."""
    due_at = job.next_run or job.schedule_value or "unscheduled"
    return f"pynchy-host-job-{safe_workflow_fragment(job.id)}-{safe_workflow_fragment(due_at)}"


def database_host_job_schedule_id(job: HostJob) -> str:
    """Return the Temporal Schedule ID for a recurring database host job."""
    return f"pynchy-host-job-schedule-{safe_workflow_fragment(job.id)}"


def config_host_cron_schedule_id(job_name: str) -> str:
    """Return the Temporal Schedule ID for a config-backed host cron job."""
    return f"pynchy-host-cron-schedule-{safe_workflow_fragment(job_name)}"


def host_git_sync_schedule_id() -> str:
    """Return the Temporal Schedule ID for host repository sync polling."""
    return HOST_GIT_SYNC_SCHEDULE_ID


def external_git_sync_schedule_id(repo_slug: str) -> str:
    """Return the Temporal Schedule ID for external repository sync polling."""
    return f"pynchy-git-sync-repo-{safe_workflow_fragment(repo_slug)}"


def channel_reconciliation_schedule_id() -> str:
    """Return the Temporal Schedule ID for channel reconciliation polling."""
    return CHANNEL_RECONCILIATION_SCHEDULE_ID


def once_due_at(value: str | None) -> datetime:
    """Return the UTC due time for a one-time scheduled item."""
    if not value:
        return datetime.now(UTC)
    due_at = datetime.fromisoformat(value)
    if due_at.tzinfo is None:
        return due_at.replace(tzinfo=UTC)
    return due_at.astimezone(UTC)


def start_delay_until(due_at: datetime) -> timedelta:
    """Return a non-negative Temporal start delay for a due time."""
    return max(due_at - datetime.now(UTC), timedelta())


def schedule_for_agent_task(task: ScheduledTask) -> Schedule:
    """Build the Temporal Schedule for a recurring agent task."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ScheduledAgentTaskWorkflow.run,
            args=[task.id],
            id=f"{agent_task_schedule_id(task)}-workflow",
            task_queue=get_settings().scheduler.temporal_task_queue,
        ),
        spec=_recurring_schedule_spec(
            task.schedule_type,
            task.schedule_value,
            timezone=_schedule_timezone(),
        ),
        policy=_schedule_policy(),
    )


def schedule_for_database_host_job(job: HostJob) -> Schedule:
    """Build the Temporal Schedule for a recurring database host job."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            DatabaseHostJobWorkflow.run,
            args=[job.id],
            id=f"{database_host_job_schedule_id(job)}-workflow",
            task_queue=get_settings().scheduler.temporal_task_queue,
        ),
        spec=_recurring_schedule_spec(
            job.schedule_type,
            job.schedule_value,
            timezone=_schedule_timezone(),
        ),
        policy=_schedule_policy(),
    )


def schedule_for_config_host_cron(job_name: str, schedule_value: str) -> Schedule:
    """Build the Temporal Schedule for a config-backed host cron job."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ConfigHostCronWorkflow.run,
            args=[job_name],
            id=f"{config_host_cron_schedule_id(job_name)}-workflow",
            task_queue=get_settings().scheduler.temporal_task_queue,
        ),
        spec=_recurring_schedule_spec("cron", schedule_value, timezone=_schedule_timezone()),
        policy=_schedule_policy(),
    )


def schedule_for_host_git_sync() -> Schedule:
    """Build the Temporal Schedule for host repository sync polling."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            HostGitSyncWorkflow.run,
            id=f"{host_git_sync_schedule_id()}-workflow",
            task_queue=get_settings().scheduler.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(seconds=HOST_GIT_SYNC_INTERVAL_SECONDS))
            ]
        ),
        policy=_schedule_policy(),
    )


def schedule_for_external_git_sync(repo_slug: str) -> Schedule:
    """Build the Temporal Schedule for one external repository sync poller."""
    schedule_id = external_git_sync_schedule_id(repo_slug)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ExternalGitSyncWorkflow.run,
            args=[repo_slug],
            id=f"{schedule_id}-workflow",
            task_queue=get_settings().scheduler.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(every=timedelta(seconds=HOST_GIT_SYNC_INTERVAL_SECONDS))
            ]
        ),
        policy=_schedule_policy(),
    )


def schedule_for_channel_reconciliation() -> Schedule:
    """Build the Temporal Schedule for channel history reconciliation."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ChannelReconciliationWorkflow.run,
            id=f"{channel_reconciliation_schedule_id()}-workflow",
            task_queue=get_settings().scheduler.temporal_task_queue,
        ),
        spec=ScheduleSpec(
            intervals=[
                ScheduleIntervalSpec(
                    every=timedelta(seconds=CHANNEL_RECONCILIATION_INTERVAL_SECONDS)
                )
            ]
        ),
        policy=_schedule_policy(),
    )


def _schedule_timezone() -> str | None:
    timezone = get_settings().timezone
    return timezone or None


def _recurring_schedule_spec(
    schedule_type: str,
    schedule_value: str,
    *,
    timezone: str | None,
) -> ScheduleSpec:
    if schedule_type == "cron":
        return ScheduleSpec(cron_expressions=[schedule_value], time_zone_name=timezone)
    if schedule_type == "interval":
        interval_ms = int(schedule_value)
        return ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(milliseconds=interval_ms))]
        )
    raise ValueError(f"Unsupported recurring schedule type: {schedule_type}")


def _schedule_policy() -> SchedulePolicy:
    return SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, pause_on_failure=False)
