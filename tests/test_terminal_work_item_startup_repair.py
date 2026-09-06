"""Historical terminal work-item provider reconciliation contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.agent_protocol.api import InFlightTurn, InFlightWorkKind
from pynchy.conversation.api import (
    ControlSurface,
    ConversationClaimId,
    ConversationControlBinding,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryIdentity,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.terminal_task_retirement import (
    retire_conversation_tasks,
    retire_provider_work_item_execution,
    retire_terminal_work_item_execution_if_unowned,
    retire_work_item_execution,
)
from pynchy.identifiers import ChatJid, GroupFolder, SessionId
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_provider_reconciliation import (
    LinearDecisionInboxRuntime,
    configure_linear_decision_inbox_runtime,
    reconcile_provider_work_item_state,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state.api import (
    apply_conversation_control_state,
    begin_in_flight_turn,
    cancel_work_item_execution,
    conversation_control_state_matches,
    create_task,
    create_work_item_claim,
    delete_workspace_profile,
    get_conversation,
    get_conversation_control_binding,
    get_in_flight_turn,
    get_latest_unresolved_work_item_transition,
    get_session,
    get_task_by_id,
    get_work_item_transition_by_request,
    get_workspace_profile,
    init_test_database,
    list_terminal_work_item_executions_needing_repair,
    list_work_item_executions,
    resolve_conversation,
    resolve_work_item_transition,
    set_conversation_control_binding,
    set_conversation_session,
    set_session,
    set_workspace_profile,
)
from pynchy.work_items.api import (
    WorkItemClaimRequest,
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransitionStatus,
)
from pynchy.workspace.api import WorkspaceProfile

_OLD_REVISION = "2026-07-29T00:00:00+00:00"
_TERMINAL_REVISION = "2026-07-30T00:00:00+00:00"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


@dataclass
class _RepairDeps:
    runtime_folders: list[GroupFolder] = field(default_factory=list)
    task_conversations: list[ConversationId] = field(default_factory=list)
    unregistered_jids: list[str] = field(default_factory=list)

    async def conversation_control_state_matches(
        self,
        conversation_id: ConversationId,
        *,
        closed: bool,
        control_state_revision: str | None,
        delivery_identity: ExternalDeliveryIdentity | None = None,
        claim_id: ConversationClaimId | None = None,
    ) -> bool:
        return await conversation_control_state_matches(
            conversation_id,
            closed=closed,
            control_state_revision=control_state_revision,
            delivery_identity=delivery_identity,
            claim_id=claim_id,
        )

    async def retire_conversation_runtime(self, folder: GroupFolder) -> None:
        self.runtime_folders.append(folder)

    async def retire_conversation_tasks(self, conversation_id: ConversationId) -> None:
        self.task_conversations.append(conversation_id)
        await retire_conversation_tasks(conversation_id)

    async def unregister_workspace(self, jid: str) -> None:
        self.unregistered_jids.append(jid)
        await delete_workspace_profile(jid)


class _ProviderClient:
    def __init__(self, issue: dict[str, object]) -> None:
        self.issue = issue
        self.issue_reads = 0

    async def get_issue(self, _issue_id: str) -> dict[str, object]:
        self.issue_reads += 1
        return self.issue

    async def query(self, _query: str, **_variables: object) -> dict[str, object]:
        raise AssertionError("terminal repair does not scan provider inboxes")

    async def create_comment(self, _issue_id: str, _body: str) -> dict[str, object]:
        raise AssertionError("terminal repair does not comment")


def _state(name: str) -> dict[str, str]:
    return {"id": f"state-{name.casefold().replace(' ', '-')}", "name": name}


def _board() -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "in_progress": _state("In Progress"),
            "human_approved": _state("Human Approved"),
            "awaiting_review": _state("Awaiting Review"),
            "follow_ups": _state("Follow Ups"),
            "blocked": _state("Blocked"),
            "done": _state("Done"),
        },
    )


async def _conversation() -> tuple[ConversationId, GroupFolder, ChatJid]:
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:project:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("project"),
    )
    folder = GroupFolder(routed_conversation_folder("project", conversation.id))
    thread_jid = ChatJid("discord:thread:issue-1")
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=conversation.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("project"),
            parent_jid=ChatJid("discord:project"),
            thread_jid=thread_jid,
            title="Terminal repair",
            updated_at=_OLD_REVISION,
        )
    )
    await apply_conversation_control_state(
        conversation.id,
        closed=False,
        control_state_revision=_OLD_REVISION,
    )
    return conversation.id, folder, thread_jid


def _task(
    conversation_id: ConversationId,
    folder: GroupFolder,
    thread_jid: ChatJid,
    *,
    task_id: str = "task-1",
) -> ScheduledTask:
    return ScheduledTask(
        id=task_id,
        group_folder=folder,
        chat_jid=thread_jid,
        prompt="Deliver the issue.",
        schedule_type="once",
        schedule_value=_TERMINAL_REVISION,
        session_policy=SessionPolicy.CONTINUE,
        status="active",
        created_at=_OLD_REVISION,
        conversation_id=str(conversation_id),
    )


async def _execution(
    status: WorkItemExecutionStatus,
    task: ScheduledTask,
    *,
    request_id: str = "claim-1",
) -> WorkItemExecution:
    execution = await create_work_item_claim(
        WorkItemClaimRequest(
            workspace="project",
            issue={
                "id": "issue-1",
                "identifier": "SYN-1",
                "url": "https://linear.app/example/issue/SYN-1",
                "updatedAt": _OLD_REVISION,
                "state": _state("Human Approved"),
            },
            turn_id=f"turn-{task.id}",
            task_id=task.id,
            initiated_by="test",
            request_id=request_id,
        )
    )
    transition = await get_work_item_transition_by_request(request_id)
    assert transition is not None
    return await resolve_work_item_transition(
        transition=transition,
        execution_status=status,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
        issue={
            "id": "issue-1",
            "identifier": "SYN-1",
            "url": "https://linear.app/example/issue/SYN-1",
            "updatedAt": _TERMINAL_REVISION,
            "state": _state(status.value),
        },
    )


async def _seed_runtime(
    conversation_id: ConversationId,
    folder: GroupFolder,
    thread_jid: ChatJid,
    task: ScheduledTask,
) -> None:
    session_id = SessionId("session-1")
    await set_workspace_profile(
        WorkspaceProfile(
            jid=thread_jid,
            name="Project/issue-1",
            folder=folder,
            trigger="@Pynchy",
        )
    )
    await set_conversation_session(conversation_id, session_id)
    await set_session(folder, session_id)
    await create_task(task)
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id=f"turn-{task.id}",
            chat_jid=thread_jid,
            group_folder=folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at=datetime.now(UTC).isoformat(),
            task_id=task.id,
            session_id=session_id,
        )
    )


def _configure_reconciliation(deps: _RepairDeps) -> None:
    configure_linear_decision_inbox_runtime(
        LinearDecisionInboxRuntime(
            list_executions=list_work_item_executions,
            list_terminal_repair_candidates=list_terminal_work_item_executions_needing_repair,
            get_latest_unresolved_transition=get_latest_unresolved_work_item_transition,
            cancel_execution=cancel_work_item_execution,
            retire_execution=retire_work_item_execution,
            retire_terminal_execution_if_unowned=retire_terminal_work_item_execution_if_unowned,
            retire_terminal_execution=partial(retire_provider_work_item_execution, deps),
        )
    )


@pytest.mark.parametrize(
    ("status", "provider_state"),
    [
        (
            WorkItemExecutionStatus.COMPLETED,
            {**_state("Archived"), "type": "completed"},
        ),
        (
            WorkItemExecutionStatus.CANCELLED,
            {**_state("Archived"), "type": "completed"},
        ),
        (
            WorkItemExecutionStatus.FAILED,
            {**_state("Archived"), "type": "completed"},
        ),
        (WorkItemExecutionStatus.COMPLETED, _state("Done")),
    ],
)
async def test_provider_terminal_reconciliation_repairs_historical_lifecycle_once(
    status: WorkItemExecutionStatus,
    provider_state: dict[str, str],
) -> None:
    conversation_id, folder, thread_jid = await _conversation()
    task = _task(conversation_id, folder, thread_jid)
    await _seed_runtime(conversation_id, folder, thread_jid, task)
    await _execution(status, task)
    deps = _RepairDeps()
    _configure_reconciliation(deps)
    client = _ProviderClient(
        {
            "id": "issue-1",
            "updatedAt": _TERMINAL_REVISION,
            "state": provider_state,
            "project": {"id": "project-1"},
        }
    )

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(return_value=True),
    ):
        assert await reconcile_provider_work_item_state(client, {"project": _board()}) == 1
        calls_after_first_repair = (
            list(deps.runtime_folders),
            list(deps.task_conversations),
            list(deps.unregistered_jids),
        )
        assert await reconcile_provider_work_item_state(client, {"project": _board()}) == 0

    conversation = await get_conversation(conversation_id)
    binding = await get_conversation_control_binding(conversation_id)
    persisted_task = await get_task_by_id(task.id)
    assert conversation is not None
    assert conversation.control_closed
    assert conversation.control_state_revision == _TERMINAL_REVISION
    assert conversation.session_id is None
    assert binding is not None
    assert binding.closed
    assert await get_session(folder) is None
    assert await get_in_flight_turn(f"turn-{task.id}") is None
    assert persisted_task is not None
    assert persisted_task.status == "cancelled"
    assert await get_workspace_profile(thread_jid) is None
    assert calls_after_first_repair == (
        deps.runtime_folders,
        deps.task_conversations,
        deps.unregistered_jids,
    )
    assert client.issue_reads == 1


@pytest.mark.parametrize(
    ("provider_state", "project_id"),
    [
        (_state("In Progress"), "project-1"),
        ({**_state("Archived"), "type": "completed"}, "unmanaged-project"),
    ],
)
async def test_candidate_without_managed_provider_terminal_preserves_route(
    provider_state: dict[str, str],
    project_id: str,
) -> None:
    conversation_id, folder, thread_jid = await _conversation()
    task = _task(conversation_id, folder, thread_jid)
    await _seed_runtime(conversation_id, folder, thread_jid, task)
    await _execution(WorkItemExecutionStatus.FAILED, task)
    deps = _RepairDeps()
    _configure_reconciliation(deps)
    client = _ProviderClient(
        {
            "id": "issue-1",
            "updatedAt": _TERMINAL_REVISION,
            "state": provider_state,
            "project": {"id": project_id},
        }
    )

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(return_value=True),
    ):
        assert await reconcile_provider_work_item_state(client, {"project": _board()}) == 0

    conversation = await get_conversation(conversation_id)
    persisted_task = await get_task_by_id(task.id)
    assert conversation is not None
    assert not conversation.control_closed
    assert conversation.session_id is not None
    assert persisted_task is not None
    assert persisted_task.status == "cancelled"
    assert deps.runtime_folders == []


async def test_provider_reconciliation_retires_orphan_exact_task_without_conversation() -> None:
    orphan_task = ScheduledTask(
        id="task-orphan",
        group_folder="project",
        chat_jid="linear:project",
        prompt="Orphaned work.",
        schedule_type="once",
        schedule_value=_TERMINAL_REVISION,
        session_policy=SessionPolicy.CONTINUE,
        created_at=_OLD_REVISION,
    )
    unrelated_task = ScheduledTask(
        id="task-unrelated",
        group_folder="other",
        chat_jid="linear:other",
        prompt="Other work.",
        schedule_type="once",
        schedule_value=_TERMINAL_REVISION,
        session_policy=SessionPolicy.CONTINUE,
        created_at=_OLD_REVISION,
    )
    await create_task(orphan_task)
    await create_task(unrelated_task)
    await _execution(WorkItemExecutionStatus.COMPLETED, orphan_task)
    deps = _RepairDeps()
    _configure_reconciliation(deps)
    client = _ProviderClient(
        {
            "id": "issue-1",
            "updatedAt": _TERMINAL_REVISION,
            "state": {**_state("Archived"), "type": "completed"},
            "project": {"id": "project-1"},
        }
    )

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(return_value=True),
    ):
        assert await reconcile_provider_work_item_state(client, {"project": _board()}) == 1

    persisted_orphan = await get_task_by_id(orphan_task.id)
    persisted_unrelated = await get_task_by_id(unrelated_task.id)
    assert persisted_orphan is not None
    assert persisted_orphan.status == "cancelled"
    assert persisted_unrelated is not None
    assert persisted_unrelated.status == "active"
    assert deps.runtime_folders == []


async def test_repair_candidate_is_ignored_after_newer_execution_reopens_issue() -> None:
    conversation_id, folder, thread_jid = await _conversation()
    terminal_task = _task(conversation_id, folder, thread_jid)
    await _seed_runtime(conversation_id, folder, thread_jid, terminal_task)
    await _execution(WorkItemExecutionStatus.COMPLETED, terminal_task)
    current_task = _task(
        conversation_id,
        folder,
        thread_jid,
        task_id="task-current",
    )
    await create_task(current_task)
    await _execution(
        WorkItemExecutionStatus.IN_PROGRESS,
        current_task,
        request_id="claim-current",
    )
    deps = _RepairDeps()
    _configure_reconciliation(deps)
    client = _ProviderClient(
        {
            "id": "issue-1",
            "updatedAt": "2026-07-30T01:00:00+00:00",
            "state": _state("In Progress"),
            "project": {"id": "project-1"},
        }
    )

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(return_value=True),
    ):
        assert await reconcile_provider_work_item_state(client, {"project": _board()}) == 1

    conversation = await get_conversation(conversation_id)
    persisted_terminal_task = await get_task_by_id(terminal_task.id)
    persisted_current_task = await get_task_by_id(current_task.id)
    assert conversation is not None
    assert not conversation.control_closed
    assert conversation.session_id is not None
    assert persisted_terminal_task is not None
    assert persisted_terminal_task.status == "cancelled"
    assert persisted_current_task is not None
    assert persisted_current_task.status == "active"
    assert await get_in_flight_turn(f"turn-{terminal_task.id}") is None
    assert deps.runtime_folders == []
    assert client.issue_reads == 1


async def test_provider_fallback_preserves_reused_task_and_turn() -> None:
    conversation_id, folder, thread_jid = await _conversation()
    shared_task = _task(conversation_id, folder, thread_jid)
    await _seed_runtime(conversation_id, folder, thread_jid, shared_task)
    terminal = await _execution(WorkItemExecutionStatus.COMPLETED, shared_task)
    await _execution(
        WorkItemExecutionStatus.IN_PROGRESS,
        shared_task,
        request_id="claim-current",
    )
    deps = _RepairDeps()

    await retire_provider_work_item_execution(deps, terminal, _TERMINAL_REVISION)

    conversation = await get_conversation(conversation_id)
    persisted_task = await get_task_by_id(shared_task.id)
    assert conversation is not None
    assert not conversation.control_closed
    assert conversation.session_id is not None
    assert persisted_task is not None
    assert persisted_task.status == "active"
    assert await get_in_flight_turn(f"turn-{shared_task.id}") is not None
    assert deps.runtime_folders == []


async def test_temporal_failure_preserves_retryable_exact_retirement() -> None:
    conversation_id, folder, thread_jid = await _conversation()
    terminal_task = _task(conversation_id, folder, thread_jid)
    await _seed_runtime(conversation_id, folder, thread_jid, terminal_task)
    await _execution(WorkItemExecutionStatus.COMPLETED, terminal_task)
    current_task = _task(
        conversation_id,
        folder,
        thread_jid,
        task_id="task-current",
    )
    await create_task(current_task)
    await _execution(
        WorkItemExecutionStatus.IN_PROGRESS,
        current_task,
        request_id="claim-current",
    )
    _configure_reconciliation(_RepairDeps())
    client = _ProviderClient(
        {
            "id": "issue-1",
            "updatedAt": "2026-07-30T01:00:00Z",
            "state": _state("In Progress"),
            "project": {"id": "project-1"},
        }
    )
    cancel_workflow = AsyncMock(
        side_effect=[RuntimeError("Temporal unavailable"), True],
    )

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        cancel_workflow,
    ):
        with pytest.raises(ExceptionGroup, match="Linear provider-state reconciliation failed"):
            await reconcile_provider_work_item_state(client, {"project": _board()})
        persisted_terminal_task = await get_task_by_id(terminal_task.id)
        assert persisted_terminal_task is not None
        assert persisted_terminal_task.status == "active"
        assert await get_in_flight_turn(f"turn-{terminal_task.id}") is not None
        assert client.issue_reads == 1

        assert await reconcile_provider_work_item_state(client, {"project": _board()}) == 1

    persisted_terminal_task = await get_task_by_id(terminal_task.id)
    persisted_current_task = await get_task_by_id(current_task.id)
    assert persisted_terminal_task is not None
    assert persisted_terminal_task.status == "cancelled"
    assert persisted_current_task is not None
    assert persisted_current_task.status == "active"
    assert await get_in_flight_turn(f"turn-{terminal_task.id}") is None
    assert client.issue_reads == 2


async def test_newer_provider_reopen_revision_wins_terminal_repair() -> None:
    conversation_id, folder, thread_jid = await _conversation()
    task = _task(conversation_id, folder, thread_jid)
    await _seed_runtime(conversation_id, folder, thread_jid, task)
    await _execution(WorkItemExecutionStatus.COMPLETED, task)
    reopen_revision = "2026-07-30T01:00:00+00:00"
    await apply_conversation_control_state(
        conversation_id,
        closed=False,
        control_state_revision=reopen_revision,
    )
    deps = _RepairDeps()
    _configure_reconciliation(deps)
    client = _ProviderClient(
        {
            "id": "issue-1",
            "updatedAt": _TERMINAL_REVISION,
            "state": {**_state("Archived"), "type": "completed"},
            "project": {"id": "project-1"},
        }
    )

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(return_value=True),
    ):
        assert await reconcile_provider_work_item_state(client, {"project": _board()}) == 1

    conversation = await get_conversation(conversation_id)
    assert conversation is not None
    assert not conversation.control_closed
    assert conversation.control_state_revision == reopen_revision
    assert conversation.session_id is not None
    assert deps.runtime_folders == []
