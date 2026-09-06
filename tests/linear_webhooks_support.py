"""Behavioral coverage for authenticated Linear issue-conversation admission."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from pynchy.conversation.models import (
    ControlSurface,
    Conversation,
    ConversationControlBinding,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.identifiers import (
    GroupFolder,
    SessionId,
)
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.state import (
    create_work_item_claim,
    get_work_item_transition_by_request,
    init_test_database,
    resolve_conversation,
    resolve_work_item_transition,
    set_conversation_control_binding,
    set_conversation_session,
)
from pynchy.work_items.api import (
    WorkItemClaimRequest,
    WorkItemExecutionStatus,
    WorkItemTransitionStatus,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from linear_webhook_test_support import (
        LinearWebhookHarness as _WebhookDeps,
    )


@dataclass(frozen=True)
class _LeaseResult:
    status: WorkItemExecutionStatus


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _workspace_board() -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={"done": {"id": "state-done"}},
    )


async def _seed_moved_active_issue(
    deps: _WebhookDeps,
    *,
    execution_status: WorkItemExecutionStatus = WorkItemExecutionStatus.IN_PROGRESS,
) -> tuple[Conversation, WorkspaceProfile]:
    destination = WorkspaceProfile(
        jid="discord:channel:destination",
        name="Destination",
        folder="destination",
        trigger="@Pynchy",
    )
    await deps.register_workspace(destination)
    original = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:org-1:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(deps.workspace.folder),
    )
    await set_conversation_session(original.id, SessionId("original-session"))
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=original.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder(deps.workspace.folder),
            parent_jid=deps.workspace.jid,
            thread_jid="discord:channel:original-thread",
            title="[PYN-1] Linear issue",
            updated_at=datetime.now(UTC).isoformat(),
        )
    )
    deps.channel.threads[deps.workspace.jid, "[PYN-1] Linear issue"] = (
        "discord:channel:original-thread"
    )
    deps.channel.closed["discord:channel:original-thread"] = False
    issue = {
        "id": "issue-1",
        "identifier": "PYN-1",
        "url": "https://linear.app/acme/issue/PYN-1",
        "updatedAt": datetime.now(UTC).isoformat(),
        "state": {"id": "state-approved", "name": "Human Approved"},
    }
    await create_work_item_claim(
        WorkItemClaimRequest(
            workspace=deps.workspace.folder,
            issue=issue,
            turn_id=None,
            task_id="linear-execute-pyn-1",
            initiated_by="linear-work-item-controller",
            request_id="pyn-1-lease",
        )
    )
    transition = await get_work_item_transition_by_request("pyn-1-lease")
    assert transition is not None
    state_names = {
        WorkItemExecutionStatus.IN_PROGRESS: ("state-progress", "In Progress"),
        WorkItemExecutionStatus.AWAITING_REVIEW: ("state-review", "Awaiting Review"),
        WorkItemExecutionStatus.FOLLOW_UPS: ("state-follow-ups", "Follow-ups"),
        WorkItemExecutionStatus.BLOCKED: ("state-blocked", "Blocked"),
    }
    state_id, state_name = state_names[execution_status]
    await resolve_work_item_transition(
        transition=transition,
        execution_status=execution_status,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
        issue={**issue, "state": {"id": state_id, "name": state_name}},
    )
    return original, destination


@asynccontextmanager
async def _linear_client_context() -> AsyncIterator[object]:
    yield object()


class _ReconcileClient(LinearClient):
    def __init__(self) -> None:
        pass


@asynccontextmanager
async def _reconcile_client_context() -> AsyncIterator[LinearClient]:
    yield _ReconcileClient()
