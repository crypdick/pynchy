"""Temporal-backed read model for scheduled-work status."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from pynchy.host.orchestrator.temporal.schedules import (
    agent_task_schedule_id,
    agent_task_workflow_id,
    database_host_job_schedule_id,
    database_host_job_workflow_id,
)
from pynchy.logger import logger
from pynchy.scheduling.api import (
    HostJob,
    ScheduledTask,
)


def _inactive_orchestration_state() -> dict[str, Any]:
    """Return the no-schedule state for definitions disabled by Pynchy config."""
    return {
        "source": "configuration",
        "state": "inactive",
        "next_run": None,
        "schedule_id": None,
        "workflow_id": None,
        "error": None,
    }


def _unavailable_orchestration_state(
    *, schedule_id: str | None, workflow_id: str | None, error: str
) -> dict[str, Any]:
    """Report unavailable Temporal state explicitly instead of using SQLite timing."""
    return {
        "source": "temporal",
        "state": "unavailable",
        "next_run": None,
        "schedule_id": schedule_id,
        "workflow_id": workflow_id,
        "error": error,
    }


def _not_scheduled_orchestration_state(
    *, schedule_id: str | None, workflow_id: str | None
) -> dict[str, Any]:
    """Report a Temporal object that has not yet been reconciled."""
    return {
        "source": "temporal",
        "state": "not_scheduled",
        "next_run": None,
        "schedule_id": schedule_id,
        "workflow_id": workflow_id,
        "error": None,
    }


def _temporal_error_state(
    exc: Exception, *, schedule_id: str | None, workflow_id: str | None
) -> dict[str, Any]:
    if isinstance(exc, RPCError) and exc.status is RPCStatusCode.NOT_FOUND:
        return _not_scheduled_orchestration_state(
            schedule_id=schedule_id,
            workflow_id=workflow_id,
        )
    return _unavailable_orchestration_state(
        schedule_id=schedule_id,
        workflow_id=workflow_id,
        error=str(exc),
    )


def _iso_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _describe_temporal_schedule(client: object, schedule_id: str) -> dict[str, Any]:
    """Read the next action from one Temporal Schedule."""
    client_any = cast("Any", client)
    try:
        description = await client_any.get_schedule_handle(schedule_id).describe(
            rpc_timeout=timedelta(seconds=2)
        )
    except Exception as exc:  # noqa: BLE001 - status exposes Temporal availability per item.
        logger.debug("Temporal Schedule status read failed", schedule_id=schedule_id, err=str(exc))
        return _temporal_error_state(exc, schedule_id=schedule_id, workflow_id=None)

    action_times = description.info.next_action_times
    next_run = _iso_timestamp(action_times[0]) if action_times else None
    return {
        "source": "temporal",
        "state": "paused" if description.schedule.state.paused else "scheduled",
        "next_run": next_run,
        "schedule_id": schedule_id,
        "workflow_id": None,
        "error": None,
    }


async def _describe_temporal_workflow(client: object, workflow_id: str) -> dict[str, Any]:
    """Read the execution time and state of one delayed Temporal workflow."""
    client_any = cast("Any", client)
    try:
        description = await client_any.get_workflow_handle(workflow_id).describe(
            rpc_timeout=timedelta(seconds=2)
        )
    except Exception as exc:  # noqa: BLE001 - status exposes Temporal availability per item.
        logger.debug("Temporal workflow status read failed", workflow_id=workflow_id, err=str(exc))
        return _temporal_error_state(exc, schedule_id=None, workflow_id=workflow_id)

    execution_time = description.execution_time
    status = description.status
    is_delayed = (
        status is WorkflowExecutionStatus.RUNNING
        and execution_time is not None
        and execution_time > datetime.now(UTC)
    )
    return {
        "source": "temporal",
        "state": "delayed" if is_delayed else (status.name.lower() if status else "unknown"),
        "next_run": _iso_timestamp(execution_time) if is_delayed else None,
        "schedule_id": None,
        "workflow_id": workflow_id,
        "error": None,
    }


async def get_temporal_orchestration_states(
    tasks: list[ScheduledTask],
    jobs: list[HostJob],
    temporal_address: str,
    temporal_namespace: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Use Temporal descriptions as the sole source of future execution state."""
    states: dict[tuple[str, str], dict[str, Any]] = {
        **{
            ("task", task.id): _inactive_orchestration_state()
            for task in tasks
            if task.status != "active"
        },
        **{
            ("host_job", job.id): _inactive_orchestration_state()
            for job in jobs
            if job.status != "active" or not job.enabled
        },
    }
    descriptions: list[tuple[tuple[str, str], str, str]] = []
    for task in tasks:
        if task.status != "active":
            continue
        if task.schedule_type == "once":
            descriptions.append((("task", task.id), "workflow", agent_task_workflow_id(task)))
        else:
            descriptions.append((("task", task.id), "schedule", agent_task_schedule_id(task)))
    for job in jobs:
        if job.status != "active" or not job.enabled:
            continue
        if job.schedule_type == "once":
            descriptions.append(
                (("host_job", job.id), "workflow", database_host_job_workflow_id(job))
            )
        else:
            descriptions.append(
                (("host_job", job.id), "schedule", database_host_job_schedule_id(job))
            )

    if not descriptions:
        return states

    try:
        client = await Client.connect(
            temporal_address,
            namespace=temporal_namespace,
            lazy=True,
        )
    except Exception as exc:  # noqa: BLE001 - status reports the Temporal connection failure.
        logger.debug("Temporal status connection failed", err=str(exc))
        for key, kind, temporal_id in descriptions:
            states[key] = _unavailable_orchestration_state(
                schedule_id=temporal_id if kind == "schedule" else None,
                workflow_id=temporal_id if kind == "workflow" else None,
                error=str(exc),
            )
        return states

    results = await asyncio.gather(
        *(
            _describe_temporal_schedule(client, temporal_id)
            if kind == "schedule"
            else _describe_temporal_workflow(client, temporal_id)
            for _key, kind, temporal_id in descriptions
        )
    )
    for (key, _kind, _temporal_id), state in zip(descriptions, results, strict=True):
        states[key] = state
    return states
