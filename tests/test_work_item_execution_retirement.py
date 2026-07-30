"""Exact durable retirement after provider-state reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from pynchy.conversation.api import (
    ConversationClaimId,
    ConversationId,
    ExternalDeliveryIdentity,
    TerminalConversationRetirement,
)
from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.host.orchestrator.terminal_task_retirement import (
    retire_provider_work_item_execution,
    retire_work_item_execution,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.work_items.api import WorkItemExecution, WorkItemExecutionStatus

if TYPE_CHECKING:
    from pynchy.identifiers import GroupFolder


class _RetirementDeps:
    async def unregister_workspace(self, _jid: str) -> None:
        raise AssertionError("stale retirement must not unregister workspaces")

    async def retire_conversation_runtime(self, _folder: GroupFolder) -> None:
        raise AssertionError("stale retirement must not stop runtime")

    async def retire_conversation_tasks(self, _conversation_id: ConversationId) -> None:
        raise AssertionError("stale retirement must not stop tasks")

    async def conversation_control_state_matches(
        self,
        _conversation_id: ConversationId,
        *,
        closed: bool,
        control_state_revision: str | None,
        delivery_identity: ExternalDeliveryIdentity | None = None,
        claim_id: ConversationClaimId | None = None,
    ) -> bool:
        del closed, control_state_revision, delivery_identity, claim_id
        raise AssertionError("stale retirement must not inspect current control")


@dataclass(frozen=True)
class _Conversation:
    id: ConversationId


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


async def test_stale_provider_terminal_snapshot_retires_only_exact_execution() -> None:
    execution = WorkItemExecution(
        id="execution-1",
        workspace="project",
        linear_issue_id="issue-1",
        linear_issue_identifier="SYN-1",
        linear_issue_url="https://linear.app/example/issue/SYN-1",
        turn_id="turn-1",
        task_id="task-1",
        attempt=1,
        flow_id=None,
        temporal_workflow_id="workflow-1",
        initiated_by="test",
        observed_state_id="state-progress",
        observed_state_name="In Progress",
        observed_updated_at=None,
        status=WorkItemExecutionStatus.COMPLETED,
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
        completed_at="2026-07-29T00:01:00+00:00",
    )
    retirement = TerminalConversationRetirement(
        runtime_folders=(),
        runtime_workspace_jids=(),
        control_state_revision="2026-07-29T00:00:00+00:00",
        is_current=False,
    )
    exact_retirement = AsyncMock()

    with (
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement.get_conversation_for_subject_key",
            AsyncMock(return_value=_Conversation(ConversationId("conversation-1"))),
        ),
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement."
            "retire_latest_terminal_work_item_conversation",
            AsyncMock(return_value=retirement),
        ),
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement."
            "retire_terminal_work_item_execution_if_unowned",
            exact_retirement,
        ),
    ):
        await retire_provider_work_item_execution(
            _RetirementDeps(),
            execution,
            "2026-07-29T00:00:00+00:00",
        )

    exact_retirement.assert_awaited_once_with(execution)
