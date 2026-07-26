"""Manual context reset terminates the Linear attempt that owns the thread."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, MagicMock, patch

from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_session_reset import (
    cancel_linear_execution_for_reset,
)
from pynchy.types import (
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkspaceProfile,
)


@dataclass(frozen=True)
class _Binding:
    conversation_id: str


@dataclass(frozen=True)
class _Subject:
    namespace: str
    key: str


@dataclass(frozen=True)
class _Conversation:
    subject: _Subject


def _execution() -> WorkItemExecution:
    return WorkItemExecution(
        id="execution-1",
        workspace="engineering",
        linear_issue_id="issue-1",
        linear_issue_identifier="SYN-89",
        linear_issue_url="https://linear.app/example/issue/SYN-89",
        turn_id="turn-1",
        task_id="task-1",
        attempt=1,
        flow_id="flow-1",
        temporal_workflow_id="pynchy-linear-task-1",
        initiated_by="webhook",
        observed_state_id="state-in-progress",
        observed_state_name="In Progress",
        observed_updated_at="2026-07-25T00:00:00Z",
        status=WorkItemExecutionStatus.IN_PROGRESS,
        summary=None,
        blocker=None,
        handoff_to=None,
        evidence_refs=(),
        requester_delivery_status="not_required",
        requester_delivery_turn_id=None,
        requester_delivery_error=None,
        requester_delivered_at=None,
        created_at="2026-07-25T00:00:00Z",
        updated_at="2026-07-25T00:00:00Z",
        completed_at=None,
    )


async def test_reset_cancels_attempt_blocks_issue_and_preserves_worktree(tmp_path) -> None:
    execution = _execution()
    worktree_file = tmp_path / "unfinished-change.txt"
    worktree_file.write_text("preserve me")
    client = MagicMock(spec=LinearClient)
    client.create_comment = AsyncMock()

    @asynccontextmanager
    async def fake_linear_client(*, workspace: str):
        assert workspace == execution.workspace
        yield client

    with (
        patch(
            "pynchy.plugins.integrations.linear_session_reset.get_conversation_control_by_thread",
            new_callable=AsyncMock,
            return_value=_Binding(conversation_id="conversation-1"),
        ),
        patch(
            "pynchy.plugins.integrations.linear_session_reset.get_conversation",
            new_callable=AsyncMock,
            return_value=_Conversation(
                subject=_Subject(
                    namespace="linear:tenant:issue",
                    key=execution.linear_issue_id,
                )
            ),
        ),
        patch(
            "pynchy.plugins.integrations.linear_session_reset.get_active_work_item_execution",
            new_callable=AsyncMock,
            return_value=execution,
        ),
        patch(
            "pynchy.plugins.integrations.linear_session_reset.cancel_scheduled_agent_workflow",
            new_callable=AsyncMock,
        ) as cancel_workflow,
        patch(
            "pynchy.plugins.integrations.linear_session_reset.cancel_task_and_checkpoint",
            new_callable=AsyncMock,
        ) as cancel_task,
        patch(
            "pynchy.plugins.integrations.linear_session_reset.linear_client",
            new=fake_linear_client,
        ),
        patch(
            "pynchy.plugins.integrations.linear_session_reset.transition_linked_work_item",
            new_callable=AsyncMock,
            return_value=replace(
                execution,
                status=WorkItemExecutionStatus.CANCELLED,
            ),
        ) as transition,
        patch(
            "pynchy.plugins.integrations.linear_session_reset.cancel_work_item_execution",
            new_callable=AsyncMock,
        ) as cancel_local,
    ):
        cancelled = await cancel_linear_execution_for_reset(
            WorkspaceProfile(
                jid="discord:channel:issue-thread",
                name="SYN-89",
                folder="linear-thread",
                trigger="@Pynchy",
            )
        )

    assert cancelled is True
    cancel_workflow.assert_awaited_once_with(execution.temporal_workflow_id)
    cancel_task.assert_awaited_once_with(execution.task_id)
    request = transition.await_args.args[3]
    assert request.target_status == "blocked"
    assert request.result_execution_status is WorkItemExecutionStatus.CANCELLED
    assert "worktree changes" in request.blocker
    assert "Human Approved" in request.blocker
    client.create_comment.assert_awaited_once_with(
        execution.linear_issue_id,
        request.blocker,
    )
    cancel_local.assert_awaited_once_with(
        execution.id,
        blocker=request.blocker,
    )
    assert worktree_file.read_text() == "preserve me"
