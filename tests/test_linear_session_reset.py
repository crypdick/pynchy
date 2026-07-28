"""Manual context reset terminates the Linear attempt that owns the thread."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from unittest.mock import AsyncMock, MagicMock, patch

from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_session_reset import (
    LinearSessionResetState,
    cancel_linear_execution_for_reset,
)
from pynchy.state import WorkItemTransitionRequest
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

    cancel_workflow = AsyncMock(return_value=True)
    get_control = AsyncMock(return_value=_Binding(conversation_id="conversation-1"))
    get_conversation = AsyncMock(
        return_value=_Conversation(
            subject=_Subject(
                namespace="linear:tenant:issue",
                key=execution.linear_issue_id,
            )
        )
    )
    get_execution = AsyncMock(return_value=execution)
    cancel_task = AsyncMock()
    cancel_local = AsyncMock()
    state = LinearSessionResetState(
        get_control_by_thread=get_control,
        get_conversation=get_conversation,
        get_active_execution=get_execution,
        cancel_task=cancel_task,
        cancel_execution=cancel_local,
        transition_request=WorkItemTransitionRequest,
    )
    with (
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
    ):
        cancelled = await cancel_linear_execution_for_reset(
            WorkspaceProfile(
                jid="discord:channel:issue-thread",
                name="SYN-89",
                folder="linear-thread",
                trigger="@Pynchy",
            ),
            cancel_scheduled_workflow=cancel_workflow,
            state=state,
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
