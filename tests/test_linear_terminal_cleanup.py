"""Terminal Linear lifecycle cleanup for routed scheduled work."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationId,
    ConversationLifecycleFence,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_http_deps
from pynchy.host.orchestrator.temporal.schedules import agent_task_workflow_id
from pynchy.host.orchestrator.temporal.workflow_control import TemporalRuntimeUnavailableError
from pynchy.host.orchestrator.webhook_conversations import ConversationWebhookDeps
from pynchy.host.orchestrator.webhook_terminal_retirement import retire_terminal_runtime
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.plugins.api import WebhookLifecycleDelivery
from pynchy.plugins.integrations.linear_webhook_effects import process_linear_webhook_lifecycle
from pynchy.plugins.integrations.linear_work_item_completion import complete_reviewed_work_item
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    admit_conversation_delivery,
    admit_webhook_receipt,
    apply_conversation_control_state,
    begin_in_flight_turn,
    begin_work_item_transition,
    bind_work_item_execution_to_task,
    cancel_work_item_execution_if_lifecycle_current,
    claim_next_conversation_delivery,
    conversation_control_state_matches,
    create_task,
    create_work_item_claim,
    get_in_flight_turn_for_group,
    get_in_flight_turn_for_task,
    get_session,
    get_task_by_id,
    get_work_item_execution_for_issue,
    get_work_item_transition_by_request,
    init_test_database,
    resolve_conversation,
    resolve_work_item_transition,
    resolve_work_item_transition_if_lifecycle_current,
    retire_conversation_for_terminal,
    set_session,
    set_workspace_profile,
)
from pynchy.state.webhook_models import WebhookReceipt
from pynchy.work_items.api import (
    WorkItemClaimRequest,
    WorkItemExecutionStatus,
    WorkItemTransitionRequest,
    WorkItemTransitionResolution,
    WorkItemTransitionStatus,
)
from pynchy.workspace.api import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _task(
    task_id: str,
    conversation_id: ConversationId,
    *,
    status: Literal["active", "paused"] = "active",
) -> ScheduledTask:
    return ScheduledTask(
        id=task_id,
        group_folder="project",
        chat_jid="discord:channel:project",
        prompt="Deliver issue.",
        schedule_type="once",
        schedule_value="2026-07-27T05:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status=status,
        created_at="2026-07-27T04:00:00+00:00",
        conversation_id=str(conversation_id),
    )


async def _conversation_id() -> ConversationId:
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:project:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("project"),
    )
    return conversation.id


async def _active_execution(
    task: ScheduledTask,
    *,
    temporal_workflow_id: str | None = None,
):
    issue = {
        "id": "issue-1",
        "identifier": "SYN-89",
        "url": "https://linear.app/example/issue/SYN-89",
        "state": {"id": "state-approved", "name": "Human Approved"},
    }
    execution = await create_work_item_claim(
        WorkItemClaimRequest(
            workspace="project",
            issue=issue,
            turn_id=None,
            task_id=task.id,
            initiated_by="test",
            request_id="claim-1",
        )
    )
    transition = await get_work_item_transition_by_request("claim-1")
    assert transition is not None
    await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.IN_PROGRESS,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )
    return await bind_work_item_execution_to_task(
        execution.id,
        task_id=task.id,
        temporal_workflow_id=temporal_workflow_id or agent_task_workflow_id(task),
    )


@dataclass
class _RuntimeRetirementDeps:
    retired_folders: list[GroupFolder] = field(default_factory=list)
    unregistered_jids: list[str] = field(default_factory=list)
    retired_conversations: list[ConversationId] = field(default_factory=list)

    async def conversation_control_state_matches(
        self,
        conversation_id: ConversationId,
        *,
        closed: bool,
        control_state_revision: str | None,
    ) -> bool:
        return await conversation_control_state_matches(
            conversation_id,
            closed=closed,
            control_state_revision=control_state_revision,
        )

    async def retire_conversation_runtime(self, folder: GroupFolder) -> None:
        self.retired_folders.append(folder)

    async def retire_conversation_tasks(self, conversation_id: ConversationId) -> None:
        self.retired_conversations.append(conversation_id)

    async def unregister_workspace(self, jid: str) -> None:
        self.unregistered_jids.append(jid)


def _terminal_delivery(
    conversation_id: ConversationId,
    *,
    state_id: str = "state-cancelled",
) -> WebhookLifecycleDelivery:
    return WebhookLifecycleDelivery(
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId("delivery-1"),
        ),
        conversation_id=conversation_id,
        subject_id="issue-1",
        workspace=GroupFolder("project"),
        context={
            "linear_state_id": state_id,
            "linear_managed_done_state_id": "state-done",
        },
    )


async def test_host_task_retirement_cancels_linked_tasks_and_workflows() -> None:
    conversation_id = await _conversation_id()
    primary = _task("primary", conversation_id)
    paused = _task("paused", conversation_id, status="paused")
    await create_task(primary)
    await create_task(paused)
    await _active_execution(primary)
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="primary-turn",
            chat_jid=primary.chat_jid,
            group_folder=primary.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at=datetime.now(UTC).isoformat(),
            task_id=primary.id,
        )
    )
    cancel_workflow = AsyncMock(return_value=True)
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        cancel_workflow,
    ):
        await deps.retire_conversation_tasks(conversation_id)

    assert {call.args[0] for call in cancel_workflow.await_args_list} == {
        agent_task_workflow_id(primary),
        agent_task_workflow_id(paused),
    }
    assert len(cancel_workflow.await_args_list) == 2
    for task_id in (primary.id, paused.id):
        task = await get_task_by_id(task_id)
        assert task is not None
        assert task.status == "cancelled"
    assert await get_in_flight_turn_for_task(primary.id) is None
    execution = await get_work_item_execution_for_issue("issue-1", workspace="project")
    assert execution is not None
    assert execution.status is WorkItemExecutionStatus.IN_PROGRESS


async def test_host_task_retirement_recovers_detached_execution_task() -> None:
    conversation_id = await _conversation_id()
    task = replace(_task("primary", conversation_id), conversation_id=None)
    await create_task(task)
    await _active_execution(task, temporal_workflow_id="linear-execution-workflow")
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="primary-turn",
            chat_jid=task.chat_jid,
            group_folder=task.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at=datetime.now(UTC).isoformat(),
            task_id=task.id,
        )
    )
    cancel_workflow = AsyncMock(return_value=True)
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        cancel_workflow,
    ):
        await deps.retire_conversation_tasks(conversation_id)

    persisted_task = await get_task_by_id(task.id)
    assert persisted_task is not None
    assert persisted_task.status == "cancelled"
    assert await get_in_flight_turn_for_task(task.id) is None
    assert {call.args[0] for call in cancel_workflow.await_args_list} == {
        agent_task_workflow_id(task),
        "linear-execution-workflow",
    }


async def test_host_task_retirement_recovers_detached_awaiting_review_task() -> None:
    conversation_id = await _conversation_id()
    task = replace(_task("primary", conversation_id), conversation_id=None)
    await create_task(task)
    await _active_execution(task, temporal_workflow_id="linear-execution-workflow")
    transition = await get_work_item_transition_by_request("claim-1")
    assert transition is not None
    await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.AWAITING_REVIEW,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )
    cancel_workflow = AsyncMock(return_value=True)
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        cancel_workflow,
    ):
        await deps.retire_conversation_tasks(conversation_id)

    persisted_task = await get_task_by_id(task.id)
    assert persisted_task is not None
    assert persisted_task.status == "cancelled"
    assert {call.args[0] for call in cancel_workflow.await_args_list} == {
        agent_task_workflow_id(task),
        "linear-execution-workflow",
    }


async def test_host_task_retirement_failure_keeps_work_retryable() -> None:
    conversation_id = await _conversation_id()
    task = _task("primary", conversation_id)
    await create_task(task)
    await _active_execution(task)
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    with (
        patch(
            "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
            AsyncMock(side_effect=TemporalRuntimeUnavailableError("unavailable")),
        ),
        pytest.raises(TemporalRuntimeUnavailableError, match="unavailable"),
    ):
        await deps.retire_conversation_tasks(conversation_id)

    persisted_task = await get_task_by_id(task.id)
    assert persisted_task is not None
    assert persisted_task.status == "active"
    execution = await get_work_item_execution_for_issue("issue-1", workspace="project")
    assert execution is not None
    assert execution.status is WorkItemExecutionStatus.IN_PROGRESS


@pytest.mark.parametrize(
    "execution_status",
    [
        WorkItemExecutionStatus.IN_PROGRESS,
        WorkItemExecutionStatus.AWAITING_REVIEW,
        WorkItemExecutionStatus.FOLLOW_UPS,
        WorkItemExecutionStatus.BLOCKED,
    ],
)
async def test_non_done_terminal_cancels_unfinished_execution(
    execution_status: WorkItemExecutionStatus,
) -> None:
    conversation_id = await _conversation_id()
    task = _task("primary", conversation_id)
    await create_task(task)
    await _active_execution(task)
    if execution_status is not WorkItemExecutionStatus.IN_PROGRESS:
        transition = await get_work_item_transition_by_request("claim-1")
        assert transition is not None
        await resolve_work_item_transition(
            transition=transition,
            execution_status=execution_status,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
        )
    await process_linear_webhook_lifecycle(_terminal_delivery(conversation_id))

    task_after = await get_task_by_id(task.id)
    assert task_after is not None
    assert task_after.status == "active"
    execution = await get_work_item_execution_for_issue("issue-1", workspace="project")
    assert execution is not None
    assert execution.status is WorkItemExecutionStatus.CANCELLED


async def test_terminal_retirement_stops_runtime_from_prior_workspace() -> None:
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:project:issue"),
        key=ConversationSubjectKey("issue-moved-before-terminal"),
    )
    conversation = await resolve_conversation(subject, GroupFolder("project-a"))
    old_folder = GroupFolder(routed_conversation_folder("project-a", conversation.id))
    old_jid = ChatJid("discord:channel:issue-moved-before-terminal")
    await set_workspace_profile(
        WorkspaceProfile(
            jid=old_jid,
            name="Project A/SYN-89",
            folder=old_folder,
            trigger="@Pynchy",
        )
    )
    await set_session(old_folder, SessionId("old-workspace-session"))
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="old-workspace-turn",
            chat_jid=old_jid,
            group_folder=old_folder,
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-27T00:00:00+00:00",
        )
    )
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId("moved-terminal"),
    )
    receipt = WebhookReceipt(
        provider=str(identity.provider),
        route=str(identity.route),
        delivery_id=str(identity.delivery_id),
        workspace="project-b",
        event_type="Issue",
        event_action="update",
        subject_id="issue-moved-before-terminal",
        payload_sha256="sha-moved-terminal",
        disposition="lifecycle",
        ignored_reason=None,
        task_id=None,
        occurred_at="2026-07-27T00:00:01+00:00",
        received_at="2026-07-27T00:00:01+00:00",
    )
    assert (await admit_webhook_receipt(receipt, None)).created
    admission = await admit_conversation_delivery(
        identity,
        subject,
        GroupFolder("project-b"),
        payload={"delivery_mode": "lifecycle"},
    )
    assert admission is not None
    deps = _RuntimeRetirementDeps()

    retirement = await retire_conversation_for_terminal(
        conversation.id,
        preserve_delivery=identity,
        control_state_revision="2026-07-27T00:00:01+00:00",
    )
    await retire_terminal_runtime(
        deps,
        conversation.id,
        retirement,
        set(),
    )

    assert old_folder in deps.retired_folders
    assert str(old_jid) in deps.unregistered_jids
    assert deps.retired_conversations == [conversation.id]
    assert await get_in_flight_turn_for_group(old_folder) is None
    assert await get_session(old_folder) is None


async def test_newer_cancelled_terminal_blocks_claimed_done_settlement() -> None:
    conversation_id = await _conversation_id()
    task = _task("primary", conversation_id)
    await create_task(task)
    execution = await _active_execution(task)
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:project:issue"),
        key=ConversationSubjectKey("issue-1"),
    )
    old_identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId("done-old"),
    )
    new_identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId("cancelled-new"),
    )
    old_revision = "2026-07-27T00:00:00+00:00"
    new_revision = "2026-07-27T00:00:01+00:00"

    async def admit_lifecycle(identity: ExternalDeliveryIdentity) -> None:
        receipt = WebhookReceipt(
            provider=str(identity.provider),
            route=str(identity.route),
            delivery_id=str(identity.delivery_id),
            workspace="project",
            event_type="Issue",
            event_action="update",
            subject_id="issue-1",
            payload_sha256=f"sha-{identity.delivery_id}",
            disposition="lifecycle",
            ignored_reason=None,
            task_id=None,
            occurred_at="2026-07-27T00:00:00+00:00",
            received_at="2026-07-27T00:00:00+00:00",
        )
        admitted_receipt = await admit_webhook_receipt(receipt, None)
        assert admitted_receipt.created
        admission = await admit_conversation_delivery(
            identity,
            subject,
            GroupFolder("project"),
            payload={"delivery_mode": "lifecycle"},
        )
        assert admission is not None

    await admit_lifecycle(old_identity)
    await apply_conversation_control_state(
        conversation_id,
        closed=True,
        control_state_revision=old_revision,
    )
    old_claim_id = ConversationClaimId("old-done-claim")
    old_claim = await claim_next_conversation_delivery(conversation_id, old_claim_id)
    assert old_claim is not None
    assert old_claim.identity == old_identity

    await admit_lifecycle(new_identity)
    retired = await retire_conversation_for_terminal(
        conversation_id,
        preserve_delivery=new_identity,
        control_state_revision=new_revision,
    )
    assert retired.is_current

    done_transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="linear-review:done-old",
            operation="complete_after_linear_done",
            target_status="done",
            result_execution_status=WorkItemExecutionStatus.COMPLETED,
        )
    )
    stale_done = await resolve_work_item_transition_if_lifecycle_current(
        WorkItemTransitionResolution(
            transition=done_transition,
            execution_status=WorkItemExecutionStatus.COMPLETED,
            transition_status=WorkItemTransitionStatus.SUCCEEDED,
        ),
        lifecycle_fence=ConversationLifecycleFence(
            conversation_id=conversation_id,
            identity=old_identity,
            claim_id=old_claim_id,
            control_state_revision=old_revision,
        ),
    )
    assert stale_done is None

    stale_cancel = await cancel_work_item_execution_if_lifecycle_current(
        execution.id,
        blocker="stale terminal state",
        lifecycle_fence=ConversationLifecycleFence(
            conversation_id=conversation_id,
            identity=old_identity,
            claim_id=old_claim_id,
            control_state_revision=old_revision,
        ),
    )
    assert stale_cancel is None

    new_claim_id = ConversationClaimId("new-cancelled-claim")
    new_claim = await claim_next_conversation_delivery(conversation_id, new_claim_id)
    assert new_claim is not None
    assert new_claim.identity == new_identity
    cancelled = await cancel_work_item_execution_if_lifecycle_current(
        execution.id,
        blocker="newer cancelled terminal state",
        lifecycle_fence=ConversationLifecycleFence(
            conversation_id=conversation_id,
            identity=new_identity,
            claim_id=new_claim_id,
            control_state_revision=new_revision,
        ),
    )

    assert cancelled is not None
    assert cancelled.status is WorkItemExecutionStatus.CANCELLED
    transition = await get_work_item_transition_by_request("linear-review:done-old")
    assert transition is not None
    assert transition.status is WorkItemTransitionStatus.PENDING


async def test_reopened_conversation_blocks_stale_done_transition_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = await _conversation_id()
    task = _task("primary", conversation_id)
    await create_task(task)
    execution = await _active_execution(task)
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId("done-old"),
    )
    receipt = WebhookReceipt(
        provider=str(identity.provider),
        route=str(identity.route),
        delivery_id=str(identity.delivery_id),
        workspace="project",
        event_type="Issue",
        event_action="update",
        subject_id="issue-1",
        payload_sha256="sha-done-old",
        disposition="lifecycle",
        ignored_reason=None,
        task_id=None,
        occurred_at="2026-07-27T00:00:00+00:00",
        received_at="2026-07-27T00:00:00+00:00",
    )
    assert (await admit_webhook_receipt(receipt, None)).created
    admission = await admit_conversation_delivery(
        identity,
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:project:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("project"),
        payload={"delivery_mode": "lifecycle"},
    )
    assert admission is not None
    old_revision = "2026-07-27T00:00:00+00:00"
    await apply_conversation_control_state(
        conversation_id,
        closed=True,
        control_state_revision=old_revision,
    )
    claim_id = ConversationClaimId("old-done-claim")
    assert await claim_next_conversation_delivery(conversation_id, claim_id)
    await apply_conversation_control_state(
        conversation_id,
        closed=False,
        control_state_revision="2026-07-27T00:00:01+00:00",
    )

    def unexpected_linear_client(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("stale Done callback must not call Linear")

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_completion.linear_client",
        unexpected_linear_client,
    )
    completed = await complete_reviewed_work_item(
        "project",
        "issue-1",
        "done-old",
        lifecycle_fence=ConversationLifecycleFence(
            conversation_id=conversation_id,
            identity=identity,
            claim_id=claim_id,
            control_state_revision=old_revision,
        ),
    )

    assert completed is None
    assert await get_work_item_transition_by_request("linear-review:done-old") is None
    current = await get_work_item_execution_for_issue("issue-1", workspace="project")
    assert current is not None
    assert current.status is WorkItemExecutionStatus.IN_PROGRESS
