"""Lifecycle coverage for IPC scheduled-task cancellation."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.host.orchestrator.api import cancel_scheduled_host_job, cancel_scheduled_task
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state import create_task

pytest_plugins = ("tests.ipc_auth_support",)


@pytest.mark.parametrize(
    ("schedule_type", "task_updates", "workflow_ids"),
    [
        ("once", {}, ["pynchy-agent-task-task-running-2025-06-01T00-00-00.000Z"]),
        (
            "once",
            {
                "occurrence_generation": 1,
                "occurrence_due_at": "2025-07-01T00:00:00.000Z",
                "superseded_occurrence_generation": 0,
                "superseded_occurrence_due_at": "2025-06-01T00:00:00.000Z",
            },
            [
                (
                    "pynchy-agent-task-task-running-988476505ecfc1dc-"
                    "2025-07-01T00-00-00.000Z-resume-1"
                ),
                "pynchy-agent-task-task-running-2025-06-01T00-00-00.000Z",
            ],
        ),
        (
            "once",
            {
                "superseded_occurrence_generation": 0,
                "superseded_occurrence_due_at": "2025-06-01T00:00:00.000Z",
            },
            ["pynchy-agent-task-task-running-2025-06-01T00-00-00.000Z"],
        ),
        ("cron", {}, ["pynchy-agent-schedule-task-running-workflow"]),
    ],
)
async def test_cancel_stops_active_execution_before_retiring_task(
    deps, monkeypatch, schedule_type, task_updates, workflow_ids
) -> None:
    task = ScheduledTask(
        id="task-running",
        group_folder="other-group",
        chat_jid="other@g.us",
        prompt="cancel me",
        schedule_type=schedule_type,
        schedule_value="2025-06-01T00:00:00.000Z",
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        status="active",
        **task_updates,
    )
    calls: list[tuple[str, str]] = []
    await create_task(task)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(side_effect=lambda called_id: calls.append(("workflow", called_id))),
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_task_and_checkpoint",
        AsyncMock(side_effect=lambda called_id: calls.append(("durable", called_id))),
    )

    await dispatch(
        {"type": "cancel_task", "taskId": task.id},
        "other-group",
        False,
        deps,
    )

    assert calls == [
        *(("workflow", workflow_id) for workflow_id in workflow_ids),
        ("durable", "task-running"),
    ]


async def test_cancel_missing_scheduled_work_is_idempotent(deps, monkeypatch) -> None:
    cancel_workflow = AsyncMock()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        cancel_workflow,
    )

    await cancel_scheduled_task("missing-task")
    await cancel_scheduled_host_job("missing-host-job")

    cancel_workflow.assert_not_awaited()
