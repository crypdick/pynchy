"""Public admission behavior at the Linear task/controller boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from pynchy.conversation.api import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
)
from pynchy.plugins.integrations.linear_planning_tasks import (
    LinearPlanningTaskRuntime,
    admit_planning_issue,
    configure_linear_planning_task_runtime,
)
from pynchy.plugins.integrations.linear_statuses import HUMAN_APPROVED_STATUS
from pynchy.plugins.integrations.linear_work_item_tasks import (
    DecisionAdmission,
    DecisionIssue,
    LinearWorkItemTaskRuntime,
    admit_decision_issue,
    configure_linear_work_item_task_runtime,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemExecution,
    WorkItemExecutionStatus,
)
from tests.linear_decision_inbox_support import _board, _DecisionClient, _issue, _Workspace

pytest_plugins = ("tests.linear_decision_inbox_support",)


def _runtime(
    *,
    active_execution=None,
    get_execution_for_issue: AsyncMock | None = None,
    get_task: AsyncMock | None = None,
    resume_once_task: AsyncMock | None = None,
    bind_execution_to_task: AsyncMock | None = None,
) -> LinearWorkItemTaskRuntime:
    return LinearWorkItemTaskRuntime(
        get_control_binding=AsyncMock(),
        get_task=get_task or AsyncMock(),
        create_task=AsyncMock(),
        update_task=AsyncMock(),
        get_task_logs=AsyncMock(),
        bind_execution_to_task=bind_execution_to_task or AsyncMock(),
        get_active_execution=AsyncMock(return_value=active_execution),
        resume_once_task=resume_once_task or AsyncMock(),
        get_execution_for_issue=get_execution_for_issue or AsyncMock(),
    )


def _planned_issue() -> DecisionIssue:
    payload = _issue(
        "issue-plan",
        "SYN-9",
        "Review the plan",
        "human_approved",
        "project-beta",
        description="<!-- pynchy.plan:start -->approved<!-- pynchy.plan:end -->",
    )
    issue = DecisionIssue.from_payload(payload)
    assert issue is not None
    return issue


def _context(client: _DecisionClient, **kwargs: object) -> DecisionAdmission:
    return DecisionAdmission(
        client=client,
        observed_at=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
        public_source=True,
        **kwargs,
    )


def _binding(*, closed: bool) -> ConversationControlBinding:
    return ConversationControlBinding(
        conversation_id=ConversationId("conversation-1"),
        surface=ControlSurface.DISCORD,
        parent_workspace="control",
        parent_jid="discord:channel:control",
        thread_jid="discord:channel:thread-1",
        title="SYN-9",
        updated_at="2026-07-31T00:00:00+00:00",
        closed=closed,
    )


def _execution() -> WorkItemExecution:
    return WorkItemExecution(
        id="execution-1",
        workspace="beta",
        linear_issue_id="issue-plan",
        linear_issue_identifier="SYN-9",
        linear_issue_url="https://linear.app/example/issue/SYN-9",
        turn_id=None,
        task_id=None,
        attempt=1,
        flow_id=None,
        temporal_workflow_id=None,
        initiated_by="test",
        observed_state_id="state-approved",
        observed_state_name="Human Approved",
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
        created_at="2026-07-31T00:00:00+00:00",
        updated_at="2026-07-31T00:00:00+00:00",
        completed_at=None,
    )


@pytest.mark.parametrize("binding", [None, "closed", "open"])
async def test_plan_review_status_delivery_respects_thread_availability(
    monkeypatch: pytest.MonkeyPatch,
    binding: object,
) -> None:
    client = _DecisionClient()
    configure_linear_work_item_task_runtime(_runtime())
    broadcast = AsyncMock()
    binding_value = {
        "closed": _binding(closed=True),
        "open": _binding(closed=False),
    }.get(binding)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None if binding is None else "conversation-1"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.get_conversation_control_binding",
        AsyncMock(return_value=binding_value),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.review_approved_plan",
        AsyncMock(return_value=None),
    )

    admitted = await admit_decision_issue(
        _planned_issue(),
        _Workspace("beta", "Beta", "linear:beta"),
        _board("project-beta"),
        HUMAN_APPROVED_STATUS,
        _context(client, broadcast_host_message=broadcast),
    )

    assert admitted is None
    if binding == "open":
        assert broadcast.await_count == 2
    else:
        broadcast.assert_not_awaited()


async def test_plan_review_status_delivery_failure_does_not_block_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    configure_linear_work_item_task_runtime(_runtime())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value="conversation-1"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.get_conversation_control_binding",
        AsyncMock(return_value=_binding(closed=False)),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.review_approved_plan",
        AsyncMock(return_value=None),
    )

    assert (
        await admit_decision_issue(
            _planned_issue(),
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            HUMAN_APPROVED_STATUS,
            _context(client, broadcast_host_message=AsyncMock(side_effect=RuntimeError("offline"))),
        )
        is None
    )


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        (1, "🔎 Checking approved plan (1/3)."),
        (2, "🔄 Retrying approved plan review (2/3)."),
        (3, "🔄 Final approved plan review attempt (3/3)."),
    ],
)
async def test_plan_review_attempt_status_is_reported_at_public_admission_boundary(
    monkeypatch: pytest.MonkeyPatch,
    attempt: int,
    expected: str,
) -> None:
    client = _DecisionClient()
    configure_linear_work_item_task_runtime(_runtime())
    broadcast = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value="conversation-1"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.get_conversation_control_binding",
        AsyncMock(return_value=_binding(closed=False)),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.review_approved_plan",
        AsyncMock(return_value=None),
    )

    assert (
        await admit_decision_issue(
            _planned_issue(),
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            HUMAN_APPROVED_STATUS,
            _context(
                client,
                broadcast_host_message=broadcast,
                plan_review_attempt=attempt,
            ),
        )
        is None
    )
    assert broadcast.await_args_list[0].args[1] == expected


async def test_human_approved_admission_ignores_existing_active_execution() -> None:
    configure_linear_work_item_task_runtime(_runtime(active_execution=Mock(id="execution-1")))

    assert (
        await admit_decision_issue(
            _planned_issue(),
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            HUMAN_APPROVED_STATUS,
            _context(_DecisionClient()),
        )
        is None
    )


async def test_plan_review_rejects_a_reviewed_issue_that_lost_its_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    configure_linear_work_item_task_runtime(_runtime())
    reviewed = _issue(
        "issue-plan",
        "SYN-9",
        "Review the plan",
        "human_approved",
        "project-beta",
        description="<!-- pynchy.plan:start -->approved<!-- pynchy.plan:end -->",
    )
    reviewed["project"] = None
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.review_approved_plan",
        AsyncMock(return_value=reviewed),
    )

    with pytest.raises(ValueError, match="no longer belongs to a project"):
        await admit_decision_issue(
            _planned_issue(),
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            HUMAN_APPROVED_STATUS,
            _context(_DecisionClient()),
        )


async def test_human_approved_admission_ignores_claim_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_linear_work_item_task_runtime(_runtime())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.acquire_work_item_lease",
        AsyncMock(side_effect=WorkItemClaimConflictError(_execution())),
    )

    assert (
        await admit_decision_issue(
            _planned_issue(),
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            HUMAN_APPROVED_STATUS,
            _context(_DecisionClient()),
        )
        is None
    )


async def test_unplanned_human_approved_admission_ignores_lease_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_linear_work_item_task_runtime(_runtime())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.acquire_work_item_lease",
        AsyncMock(side_effect=WorkItemClaimConflictError(_execution())),
    )
    payload = _issue(
        "issue-approved",
        "SYN-10",
        "Start approved work",
        "human_approved",
        "project-beta",
    )
    issue = DecisionIssue.from_payload(payload)
    assert issue is not None

    assert (
        await admit_decision_issue(
            issue,
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            HUMAN_APPROVED_STATUS,
            _context(_DecisionClient()),
        )
        is None
    )


async def test_human_approved_admission_ignores_non_running_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_linear_work_item_task_runtime(_runtime())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None),
    )
    lease = Mock(status=WorkItemExecutionStatus.UNKNOWN)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.acquire_work_item_lease",
        AsyncMock(return_value=lease),
    )
    issue = DecisionIssue.from_payload(
        _issue(
            "issue-approved",
            "SYN-10",
            "Start work",
            "human_approved",
            "project-beta",
        )
    )
    assert issue is not None

    assert (
        await admit_decision_issue(
            issue,
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            HUMAN_APPROVED_STATUS,
            _context(_DecisionClient()),
        )
        is None
    )


async def test_follow_up_admission_defers_an_uncertain_execution() -> None:
    latest = Mock(id="execution-1", status=WorkItemExecutionStatus.UNKNOWN)
    configure_linear_work_item_task_runtime(
        _runtime(get_execution_for_issue=AsyncMock(return_value=latest))
    )
    issue = DecisionIssue.from_payload(
        _issue("issue-follow-up", "SYN-11", "Follow up", "follow_ups", "project-beta")
    )
    assert issue is not None

    assert (
        await admit_decision_issue(
            issue,
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            "follow_ups",
            _context(_DecisionClient()),
        )
        is None
    )


async def test_in_progress_admission_binds_a_paused_task_when_resume_stays_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution = replace(
        _execution(),
        linear_issue_id="issue-progress",
        linear_issue_identifier="SYN-12",
        linear_issue_url="https://linear.app/example/issue/SYN-12",
        task_id="task-1",
    )
    paused = ScheduledTask(
        id="task-1",
        group_folder="beta",
        chat_jid="linear:beta",
        prompt="existing",
        schedule_type="once",
        schedule_value="2026-07-31T00:05:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status="paused",
        last_run="2026-07-30T00:00:00+00:00",
        derived_thread_name="[SYN-12] Work",
    )
    bind = AsyncMock()
    configure_linear_work_item_task_runtime(
        _runtime(
            active_execution=execution,
            get_task=AsyncMock(return_value=paused),
            resume_once_task=AsyncMock(return_value=True),
            bind_execution_to_task=bind,
        )
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.ensure_task_active",
        AsyncMock(return_value=(paused, False)),
    )
    issue = DecisionIssue.from_payload(
        _issue("issue-progress", "SYN-12", "Resume work", "in_progress", "project-beta")
    )
    assert issue is not None

    assert (
        await admit_decision_issue(
            issue,
            _Workspace("beta", "Beta", "linear:beta"),
            _board("project-beta"),
            "in_progress",
            _context(_DecisionClient()),
        )
        is None
    )
    bind.assert_awaited_once()


async def test_planning_admission_resumes_a_quiet_paused_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = DecisionIssue.from_payload(
        _issue(
            "issue-planning",
            "SYN-13",
            "Recover planning",
            "ready_for_planning",
            "project-beta",
        )
    )
    assert issue is not None
    paused = ScheduledTask(
        id="linear-plan-syn-13-existing",
        group_folder="beta",
        chat_jid="linear:beta",
        prompt='{"issue_id": "issue-planning"}',
        schedule_type="once",
        schedule_value="2026-07-31T00:05:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status="paused",
        last_run="2026-07-30T00:00:00+00:00",
        created_at="2026-07-30T00:00:00+00:00",
        derived_thread_name="[SYN-13] Recover planning",
    )
    active = replace(paused, status="active")
    get_task = AsyncMock(side_effect=[paused, active])
    resume_once_task = AsyncMock(return_value=True)
    configure_linear_work_item_task_runtime(
        _runtime(get_task=get_task, resume_once_task=resume_once_task)
    )
    configure_linear_planning_task_runtime(
        LinearPlanningTaskRuntime(get_all_tasks=AsyncMock(return_value=[paused]))
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_planning_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None),
    )

    task = await admit_planning_issue(
        issue,
        _Workspace("beta", "Beta", "linear:beta"),
        observed_at=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
        public_source=True,
    )

    assert task == active
    resume_once_task.assert_awaited_once_with(paused.id)


async def test_planning_admission_keeps_a_recently_run_paused_task_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = DecisionIssue.from_payload(
        _issue(
            "issue-planning",
            "SYN-14",
            "Do not bypass circuit breaker",
            "ready_for_planning",
            "project-beta",
        )
    )
    assert issue is not None
    paused = ScheduledTask(
        id="linear-plan-syn-14-existing",
        group_folder="beta",
        chat_jid="linear:beta",
        prompt='{"issue_id": "issue-planning"}',
        schedule_type="once",
        schedule_value="2026-07-31T00:05:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status="paused",
        last_run="2026-07-31T00:01:00+00:00",
        created_at="2026-07-30T00:00:00+00:00",
        derived_thread_name="[SYN-14] Do not bypass circuit breaker",
    )
    resume_once_task = AsyncMock(return_value=True)
    configure_linear_work_item_task_runtime(
        _runtime(get_task=AsyncMock(return_value=paused), resume_once_task=resume_once_task)
    )
    configure_linear_planning_task_runtime(
        LinearPlanningTaskRuntime(get_all_tasks=AsyncMock(return_value=[paused]))
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_planning_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None),
    )

    task = await admit_planning_issue(
        issue,
        _Workspace("beta", "Beta", "linear:beta"),
        observed_at=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
        public_source=True,
    )

    assert task is None
    resume_once_task.assert_not_awaited()


async def test_planning_admission_keeps_a_paused_task_when_guard_refuses_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue = DecisionIssue.from_payload(
        _issue(
            "issue-planning",
            "SYN-15",
            "Respect active turn",
            "ready_for_planning",
            "project-beta",
        )
    )
    assert issue is not None
    paused = ScheduledTask(
        id="linear-plan-syn-15-existing",
        group_folder="beta",
        chat_jid="linear:beta",
        prompt='{"issue_id": "issue-planning"}',
        schedule_type="once",
        schedule_value="2026-07-31T00:05:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        status="paused",
        last_run="2026-07-30T00:00:00+00:00",
        created_at="2026-07-30T00:00:00+00:00",
        derived_thread_name="[SYN-15] Respect active turn",
    )
    resume_once_task = AsyncMock(return_value=False)
    configure_linear_work_item_task_runtime(
        _runtime(get_task=AsyncMock(return_value=paused), resume_once_task=resume_once_task)
    )
    configure_linear_planning_task_runtime(
        LinearPlanningTaskRuntime(get_all_tasks=AsyncMock(return_value=[paused]))
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_planning_tasks.linear_issue_conversation_id",
        AsyncMock(return_value=None),
    )

    task = await admit_planning_issue(
        issue,
        _Workspace("beta", "Beta", "linear:beta"),
        observed_at=datetime(2026, 7, 31, 0, 5, tzinfo=UTC),
        public_source=True,
    )

    assert task is None
    resume_once_task.assert_awaited_once_with(paused.id)
