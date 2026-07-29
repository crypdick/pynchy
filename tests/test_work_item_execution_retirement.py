"""Exact durable retirement after provider-state reconciliation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.host.orchestrator.terminal_task_retirement import retire_work_item_execution
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.work_items.api import WorkItemExecution, WorkItemExecutionStatus


async def test_retirement_cancels_only_the_execution_runtime() -> None:
    task = ScheduledTask(
        id="task-1",
        group_folder="project",
        chat_jid="linear:project",
        prompt="Deliver issue.",
        schedule_type="once",
        schedule_value="2026-07-29T00:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status="active",
        created_at="2026-07-29T00:00:00+00:00",
    )
    execution = WorkItemExecution(
        id="execution-1",
        workspace="project",
        linear_issue_id="issue-1",
        linear_issue_identifier="SYN-1",
        linear_issue_url="https://linear.app/example/issue/SYN-1",
        turn_id="turn-1",
        task_id=task.id,
        attempt=1,
        flow_id=None,
        temporal_workflow_id="linear-workflow-1",
        initiated_by="test",
        observed_state_id="state-progress",
        observed_state_name="In Progress",
        observed_updated_at=None,
        status=WorkItemExecutionStatus.IN_PROGRESS,
        summary=None,
        blocker=None,
        handoff_to=None,
        evidence_refs=(),
        requester_delivery_status="not_requested",
        requester_delivery_turn_id=None,
        requester_delivery_error=None,
        requester_delivered_at=None,
        created_at="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
        completed_at=None,
    )
    cancel_workflow = AsyncMock(return_value=True)
    cancel_task = AsyncMock()
    clear_turn = AsyncMock()

    with (
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement.get_task_by_id",
            AsyncMock(return_value=task),
        ),
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
            cancel_workflow,
        ),
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement.cancel_task_and_checkpoint",
            cancel_task,
        ),
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement.clear_in_flight_turn",
            clear_turn,
        ),
    ):
        await retire_work_item_execution(execution)

    assert {call.args[0] for call in cancel_workflow.await_args_list} == {
        agent_task_workflow_id(task),
        execution.temporal_workflow_id,
    }
    cancel_task.assert_awaited_once_with(task.id)
    clear_turn.assert_awaited_once_with("turn-1")
