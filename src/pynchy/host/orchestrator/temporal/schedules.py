"""Temporal Schedule construction for Pynchy scheduled work."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)

from pynchy.host.orchestrator.scheduler_deps import (
    SchedulerRuntimeConfig,  # noqa: TC001, RUF100 - beartype resolves Temporal schedule annotations at runtime.
)
from pynchy.host.orchestrator.temporal.workflows import (
    CanaryRunWorkflow,
    ChannelReconciliationWorkflow,
    ConfigHostCronWorkflow,
    DatabaseHostJobWorkflow,
    ExternalGitSyncWorkflow,
    HostGitSyncWorkflow,
    LinearWorkItemReconciliationWorkflow,
    ScheduledAgentTaskWorkflow,
)
from pynchy.scheduling.api import (  # noqa: TC001, RUF100 - beartype resolves Temporal schedule annotations at runtime.
    HostJob,
    ScheduledTask,
    agent_task_workflow_id,
    safe_workflow_fragment,
)

SCHEDULE_PREFIXES = (
    "pynchy-agent-schedule-",
    "pynchy-host-job-schedule-",
    "pynchy-host-cron-schedule-",
    "pynchy-git-sync-",
    "pynchy-channel-reconciliation",
    "pynchy-linear-work-item-reconciliation",
    "pynchy-canary-",
)
HOST_GIT_SYNC_SCHEDULE_ID = "pynchy-git-sync-host"
CHANNEL_RECONCILIATION_SCHEDULE_ID = "pynchy-channel-reconciliation"
LINEAR_WORK_ITEM_RECONCILIATION_SCHEDULE_ID = "pynchy-linear-work-item-reconciliation"
CANARY_SCHEDULE_ID = "pynchy-canary-schedule"
LINEAR_WORK_ITEM_RECONCILIATION_INTERVAL = timedelta(minutes=1)
_UNSUPPORTED_RECURRING_SCHEDULE_TYPE = "Unsupported recurring schedule type: {schedule_type}"


def is_stale_agent_task_once_workflow(task: ScheduledTask, workflow_id: str) -> bool:
    """Return whether a one-shot execution mismatches the task's current definition."""
    return workflow_id.startswith("pynchy-agent-task-") and (
        task.schedule_type != "once" or workflow_id != agent_task_workflow_id(task)
    )


def agent_task_schedule_id(task: ScheduledTask) -> str:
    """Return the Temporal Schedule ID for a recurring agent task."""
    return f"pynchy-agent-schedule-{safe_workflow_fragment(task.id)}"


def database_host_job_workflow_id(job: HostJob) -> str:
    """Return the idempotency key for a one-time database host job."""
    due_at = job.schedule_value
    return f"pynchy-host-job-{safe_workflow_fragment(job.id)}-{safe_workflow_fragment(due_at)}"


def is_stale_database_host_job_once_workflow(job: HostJob, workflow_id: str) -> bool:
    """Return whether a one-shot execution mismatches the host job's current definition."""
    return (
        workflow_id.startswith("pynchy-host-job-")
        and not workflow_id.startswith("pynchy-host-job-schedule-")
        and (job.schedule_type != "once" or workflow_id != database_host_job_workflow_id(job))
    )


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


def linear_work_item_reconciliation_schedule_id() -> str:
    """Return the Temporal Schedule ID for managed Linear work recovery."""
    return LINEAR_WORK_ITEM_RECONCILIATION_SCHEDULE_ID


def canary_schedule_id() -> str:
    """Return the Temporal Schedule ID for the external-service canary runner."""
    return CANARY_SCHEDULE_ID


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


def schedule_for_agent_task(task: ScheduledTask, runtime: SchedulerRuntimeConfig) -> Schedule:
    """Build the Temporal Schedule for a recurring agent task."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ScheduledAgentTaskWorkflow.run,
            args=[task.id],
            id=f"{agent_task_schedule_id(task)}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=_recurring_schedule_spec(
            task.schedule_type,
            task.schedule_value,
            timezone=runtime.timezone,
        ),
        policy=_agent_task_schedule_policy(task),
    )


def schedule_for_database_host_job(job: HostJob, runtime: SchedulerRuntimeConfig) -> Schedule:
    """Build the Temporal Schedule for a recurring database host job."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            DatabaseHostJobWorkflow.run,
            args=[job.id],
            id=f"{database_host_job_schedule_id(job)}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=_recurring_schedule_spec(
            job.schedule_type,
            job.schedule_value,
            timezone=runtime.timezone,
        ),
        policy=_schedule_policy(),
    )


def schedule_for_config_host_cron(
    job_name: str, schedule_value: str, runtime: SchedulerRuntimeConfig
) -> Schedule:
    """Build the Temporal Schedule for a config-backed host cron job."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ConfigHostCronWorkflow.run,
            args=[job_name],
            id=f"{config_host_cron_schedule_id(job_name)}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=_recurring_schedule_spec("cron", schedule_value, timezone=runtime.timezone),
        policy=_schedule_policy(),
    )


def schedule_for_host_git_sync(runtime: SchedulerRuntimeConfig) -> Schedule:
    """Build the Temporal Schedule for host repository sync polling."""
    interval = timedelta(seconds=runtime.git_sync_interval_seconds)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            HostGitSyncWorkflow.run,
            id=f"{host_git_sync_schedule_id()}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)]),
        policy=_poller_schedule_policy(interval),
    )


def schedule_for_external_git_sync(repo_slug: str, runtime: SchedulerRuntimeConfig) -> Schedule:
    """Build the Temporal Schedule for one external repository sync poller."""
    schedule_id = external_git_sync_schedule_id(repo_slug)
    interval = timedelta(seconds=runtime.git_sync_interval_seconds)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ExternalGitSyncWorkflow.run,
            args=[repo_slug],
            id=f"{schedule_id}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)]),
        policy=_poller_schedule_policy(interval),
    )


def schedule_for_channel_reconciliation(runtime: SchedulerRuntimeConfig) -> Schedule:
    """Build the Temporal Schedule for channel history reconciliation."""
    interval = timedelta(seconds=runtime.channel_reconciliation_interval_seconds)
    return Schedule(
        action=ScheduleActionStartWorkflow(
            ChannelReconciliationWorkflow.run,
            id=f"{channel_reconciliation_schedule_id()}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)]),
        policy=_poller_schedule_policy(interval),
    )


def schedule_for_linear_work_item_reconciliation(runtime: SchedulerRuntimeConfig) -> Schedule:
    """Build the periodic managed Linear work-item recovery schedule."""
    interval = LINEAR_WORK_ITEM_RECONCILIATION_INTERVAL
    return Schedule(
        action=ScheduleActionStartWorkflow(
            LinearWorkItemReconciliationWorkflow.run,
            id=f"{linear_work_item_reconciliation_schedule_id()}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=interval)]),
        policy=_poller_schedule_policy(interval),
    )


def schedule_for_canaries(runtime: SchedulerRuntimeConfig) -> Schedule:
    """Build the configured recurring schedule for external-service canaries."""
    return Schedule(
        action=ScheduleActionStartWorkflow(
            CanaryRunWorkflow.run,
            id=f"{canary_schedule_id()}-workflow",
            task_queue=runtime.temporal_task_queue,
        ),
        spec=_recurring_schedule_spec(
            "cron",
            runtime.canary_schedule,
            timezone=runtime.timezone,
        ),
        policy=_schedule_policy(),
    )


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
    raise ValueError(_UNSUPPORTED_RECURRING_SCHEDULE_TYPE.format(schedule_type=schedule_type))


def _schedule_policy() -> SchedulePolicy:
    return SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, pause_on_failure=False)


def _poller_schedule_policy(interval: timedelta) -> SchedulePolicy:
    """Prevent stale reconciliation polls from replaying after an outage."""
    return SchedulePolicy(
        overlap=ScheduleOverlapPolicy.SKIP,
        catchup_window=interval,
        pause_on_failure=False,
    )


def _agent_task_schedule_policy(task: ScheduledTask) -> SchedulePolicy:
    """Keep config jobs serial without suppressing one pending occurrence.

    Config jobs have one derived task thread. Buffering one occurrence ensures
    that an overrun remains in that same thread after the current run finishes.
    Database-created direct tasks preserve the existing skip-on-overlap policy.
    """
    if task.config_job_name is not None:
        return SchedulePolicy(overlap=ScheduleOverlapPolicy.BUFFER_ONE, pause_on_failure=False)
    return _schedule_policy()
