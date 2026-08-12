"""Behavioral tests for host-leased Linear execution admission."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, call

from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewResult,
)
from pynchy.plugins.integrations.linear_decision_inbox import reconcile_linear_decision_inbox
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    WorkItemTransitionRequest,
    begin_work_item_transition,
    create_task,
    get_active_work_item_execution,
    get_all_tasks,
    get_task_by_id,
    get_work_item_execution_for_task,
    get_work_item_transition_by_request,
    resolve_work_item_transition,
)
from pynchy.work_items.api import (
    WorkItemExecutionStatus,
    WorkItemTransitionStatus,
)
from tests.linear_decision_inbox_support import (
    _board,
    _ControlBinding,
    _DecisionClient,
    _issue,
    _LinearAccount,
    _PagedDecisionClient,
    _state,
    _Workspace,
)

pytest_plugins = ("tests.linear_decision_inbox_support",)


def _prompt_context(task: ScheduledTask) -> dict[str, object]:
    payload = json.loads(task.prompt[task.prompt.index("{") : task.prompt.rindex("}") + 1])
    assert isinstance(payload, dict)
    return payload


if TYPE_CHECKING:
    import pytest


async def test_reconcile_leases_authorized_work_before_admitting_one_task() -> None:
    client = _DecisionClient()
    workspaces = [
        _Workspace("alpha", "Alpha", "linear:alpha"),
        _Workspace("beta", "Beta", "linear:beta"),
    ]
    boards = {
        "alpha": _board("project-alpha"),
        "beta": _board("project-beta"),
    }
    now = datetime(2026, 7, 19, 8, 5, tzinfo=UTC)
    review_plan = AsyncMock()

    created = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=now,
        review_plan=review_plan,
    )
    duplicate = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=now,
        review_plan=review_plan,
    )

    review_plan.assert_not_awaited()
    assert duplicate == []
    assert len(created) == 1
    task = created[0]
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    assert execution.status.value == "in_progress"
    assert execution.task_id == task.id
    assert await get_work_item_execution_for_task(task.id) == execution
    assert execution.initiated_by == "linear-work-item-controller"
    assert execution.temporal_workflow_id is not None
    assert execution.temporal_workflow_id.startswith("pynchy-agent-task-")
    assert task.group_folder == "beta"
    assert task.input_source == "external:linear:authorized"
    assert task.session_policy is SessionPolicy.CONTINUE
    assert task.derived_thread_name == "[SYN-3] Execute an approved task"
    assert _prompt_context(task)["issue_id"] == "issue-execute"
    assert _prompt_context(task)["identifier"] == "SYN-3"
    assert len(await get_all_tasks()) == 1


async def test_reconcile_defers_in_progress_and_follow_up_work_with_uncertain_execution() -> None:
    client = _DecisionClient()
    workspaces = [_Workspace("beta", "Beta", "linear:beta")]
    boards = {"beta": _board("project-beta")}
    created = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="execution-uncertain",
            operation="provider_callback",
            target_status="unknown",
            result_execution_status=WorkItemExecutionStatus.UNKNOWN,
        )
    )
    await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.UNKNOWN,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )

    issue = client.issues_by_state["state-approved"].pop()
    issue["state"] = _state("in_progress")
    client.issues_by_state["state-progress"].append(issue)
    assert (
        await reconcile_linear_decision_inbox(
            client,
            workspaces,
            boards,
            now=datetime(2026, 7, 19, 8, 6, tzinfo=UTC),
        )
        == []
    )

    issue = client.issues_by_state["state-progress"].pop()
    issue["state"] = _state("follow_ups")
    client.issues_by_state["state-follow-ups"].append(issue)
    assert (
        await reconcile_linear_decision_inbox(
            client,
            workspaces,
            boards,
            now=datetime(2026, 7, 19, 8, 7, tzinfo=UTC),
        )
        == []
    )
    assert created


async def test_planned_work_is_reviewed_before_execution_lease() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"][0]["description"] = (
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Implement the approved change.\n"
        "<!-- pynchy.plan:end -->"
    )
    reviewer = AsyncMock(
        return_value=LinearPlanReviewResult(
            LinearPlanReviewDecision.PROCEED,
            "The plan still matches the current implementation.",
        )
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        review_plan=reviewer,
    )

    assert len(created) == 1
    request = reviewer.await_args.args[0]
    assert request.issue_id == "issue-execute"
    assert request.workspace == "beta"
    assert await get_active_work_item_execution("issue-execute") is not None


async def test_minor_plan_amendment_updates_plan_and_still_leases_work() -> None:
    client = _DecisionClient()
    issue = client.issues_by_state["state-approved"][0]
    issue["description"] = (
        "Keep this context.\n\n"
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Call the renamed helper by its old name.\n"
        "<!-- pynchy.plan:end -->"
    )
    reviewer = AsyncMock(
        return_value=LinearPlanReviewResult(
            LinearPlanReviewDecision.AMEND,
            "The helper was renamed without changing behavior.",
            "Call the helper by its current name, then run the existing regression.",
        )
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        review_plan=reviewer,
    )

    assert len(created) == 1
    assert await get_active_work_item_execution("issue-execute") is not None
    assert issue["state"]["id"] == "state-progress"
    assert "Call the helper by its current name" in issue["description"]
    assert "old name" not in issue["description"]
    assert client.comments == [
        (
            "issue-execute",
            (
                "Plan freshness review applied a non-material amendment, "
                "so execution will continue.\n\n"
                "Reason: The helper was renamed without changing behavior."
            ),
        )
    ]


async def test_planned_work_reports_actual_review_status_to_its_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"][0]["description"] = (
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Implement the approved change.\n"
        "<!-- pynchy.plan:end -->"
    )
    reviewer = AsyncMock(
        return_value=LinearPlanReviewResult(
            LinearPlanReviewDecision.PROCEED,
            "The plan still matches the current implementation.",
        )
    )
    broadcaster = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_issue_conversation_id",
        AsyncMock(return_value="conversation-1"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.get_conversation_control_binding",
        AsyncMock(
            return_value=_ControlBinding(
                thread_jid="discord:channel:issue-1",
                closed=False,
            )
        ),
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        review_plan=reviewer,
        broadcast_host_message=broadcaster,
    )

    assert len(created) == 1
    assert broadcaster.await_args_list == [
        call(
            "discord:channel:issue-1",
            "🔎 Rechecking the approved plan against the current checkout.",
        ),
        call("discord:channel:issue-1", "✅ Plan check passed. Starting work."),
    ]


async def test_reconcile_continues_after_one_issue_admission_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    client.issues_by_state["state-ready"] = [
        _issue(
            "issue-bad-runtime",
            "SYN-99",
            "Leave this malformed runtime alone",
            "ready_for_planning",
            "project-beta",
        )
    ]

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.admit_planning_issue",
        AsyncMock(
            side_effect=RuntimeError(
                "Provider subject key resolves to multiple routed conversations"
            )
        ),
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
    )

    assert len(created) == 1
    assert created[0].id.startswith("linear-execute-syn-3-")


async def test_stale_plan_is_replaced_without_acquiring_a_lease() -> None:
    client = _DecisionClient()
    issue = client.issues_by_state["state-approved"][0]
    issue["description"] = (
        "Keep this context.\n\n"
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Use the removed module.\n"
        "<!-- pynchy.plan:end -->"
    )
    reviewer = AsyncMock(
        return_value=LinearPlanReviewResult(
            LinearPlanReviewDecision.REPLAN,
            "The named module no longer exists.",
            "Use the current shared admission path.",
        )
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        review_plan=reviewer,
    )

    assert created == []
    assert await get_active_work_item_execution("issue-execute") is None
    assert issue["state"]["id"] == "state-awaiting-plan"
    assert "Use the current shared admission path." in issue["description"]
    assert client.comments == [
        (
            "issue-execute",
            (
                "Plan freshness review found that the approved plan is materially stale, "
                "so execution was not leased.\n\nReason: The named module no longer exists."
                "\n\nThe replacement plan is awaiting review."
            ),
        )
    ]


async def test_failed_plan_reviewer_keeps_issue_approved_for_temporal_retry() -> None:
    client = _DecisionClient()
    issue = client.issues_by_state["state-approved"][0]
    issue["description"] = (
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Implement the approved change.\n"
        "<!-- pynchy.plan:end -->"
    )
    reviewer = AsyncMock(side_effect=RuntimeError("plan reviewer failed"))

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        review_plan=reviewer,
    )

    assert created == []
    assert await get_active_work_item_execution("issue-execute") is None
    assert issue["state"]["id"] == "state-approved"
    assert client.comments == []


async def test_missing_plan_reviewer_keeps_issue_approved_for_temporal_retry() -> None:
    client = _DecisionClient()
    issue = client.issues_by_state["state-approved"][0]
    issue["description"] = (
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Implement the approved change.\n"
        "<!-- pynchy.plan:end -->"
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
    )

    assert created == []
    assert await get_active_work_item_execution("issue-execute") is None
    assert issue["state"]["id"] == "state-approved"
    assert client.comments == []


async def test_reconcile_recovers_ready_planning_without_execution_authority() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = []
    client.issues_by_state["state-ready"] = [
        _issue(
            "issue-plan",
            "SYN-89",
            "Plan terminal thread archival",
            "ready_for_planning",
            "project-beta",
        ),
        _issue(
            "issue-unmanaged-plan",
            "SYN-40",
            "Ignore an unmanaged blog item",
            "ready_for_planning",
            "project-unmanaged",
        ),
    ]
    now = datetime(2026, 7, 25, 20, 5, tzinfo=UTC)

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=now,
    )
    duplicate = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=now,
    )

    assert len(created) == 1
    task = created[0]
    assert task.id.startswith("linear-plan-syn-89-")
    assert task.input_source == "external:linear:ready_for_planning"
    context = _prompt_context(task)
    assert context["issue_id"] == "issue-plan"
    assert context["observed_state"] == "ready_for_planning"
    assert duplicate == []
    assert await get_active_work_item_execution("issue-plan") is None


async def test_planning_and_execution_reuse_one_issue_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = []
    issue = _issue(
        "issue-plan",
        "SYN-89",
        "Unify the issue runtime",
        "ready_for_planning",
        "project-beta",
    )
    client.issues_by_state["state-ready"] = [issue]
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_account_for_workspace",
        lambda _workspace: _LinearAccount(name="linear"),
    )
    workspaces = [_Workspace("beta", "Beta", "linear:beta")]
    boards = {"beta": _board("project-beta")}

    planning = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=datetime(2026, 7, 25, 20, 5, tzinfo=UTC),
    )
    client.issues_by_state["state-ready"].remove(issue)
    issue["state"] = _state("human_approved")
    issue["updatedAt"] = "2026-07-25T21:00:00+00:00"
    client.issues_by_state["state-approved"].append(issue)
    execution = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=datetime(2026, 7, 25, 21, 5, tzinfo=UTC),
    )

    assert len(planning) == 1
    assert len(execution) == 1
    assert planning[0].id != execution[0].id
    assert planning[0].conversation_id is not None
    assert execution[0].conversation_id == planning[0].conversation_id
    assert planning[0].session_policy is SessionPolicy.CONTINUE
    assert execution[0].session_policy is SessionPolicy.CONTINUE


async def test_reconcile_reactivates_a_quiet_ready_planning_task() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = []
    client.issues_by_state["state-ready"] = [
        _issue(
            "issue-plan",
            "SYN-89",
            "Plan terminal thread archival",
            "ready_for_planning",
            "project-beta",
        )
    ]
    await create_task(
        ScheduledTask(
            id="linear-ready-for-planning-syn-89-existing",
            group_folder="beta",
            chat_jid="linear:beta",
            prompt=(
                "[Source: linear-decision-inbox]\n"
                '{"identifier": "SYN-89", "issue_id": "issue-plan"}'
            ),
            schedule_type="once",
            schedule_value="2026-07-25T19:00:00+00:00",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            status="completed",
            last_run="2026-07-25T19:00:00+00:00",
            created_at="2026-07-25T19:00:00+00:00",
        )
    )

    recovered = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 25, 20, 5, tzinfo=UTC),
    )

    assert [task.id for task in recovered] == ["linear-ready-for-planning-syn-89-existing"]
    task = await get_task_by_id("linear-ready-for-planning-syn-89-existing")
    assert task is not None
    assert task.status == "active"
    assert _prompt_context(task)["issue_id"] == "issue-plan"


async def test_private_account_keeps_authorized_context_trusted() -> None:
    created = await reconcile_linear_decision_inbox(
        _DecisionClient(),
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
        public_source=False,
    )

    task = created[0]
    assert task.input_source == "trusted:linear:authorized"
    assert _prompt_context(task)["issue_id"] == "issue-execute"


async def test_reconcile_admits_follow_up_work_without_a_second_approval_lease() -> None:
    client = _DecisionClient()
    follow_up = _issue(
        "issue-follow-up",
        "SYN-5",
        "Finish operational cleanup",
        "follow_ups",
        "project-beta",
    )
    client.issues_by_state["state-follow-ups"].append(follow_up)

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
        public_source=False,
    )
    duplicate = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 6, tzinfo=UTC),
        public_source=False,
    )

    follow_up_tasks = [task for task in created if task.id.startswith("linear-follow-ups-")]
    assert len(follow_up_tasks) == 1
    assert follow_up_tasks[0].input_source == "trusted:linear:follow-ups"
    assert _prompt_context(follow_up_tasks[0])["issue_id"] == "issue-follow-up"
    assert await get_active_work_item_execution("issue-follow-up") is None
    assert duplicate == []


async def test_reauthorized_handoff_creates_a_new_execution_task() -> None:
    client = _DecisionClient()
    workspaces = [_Workspace("beta", "Beta", "linear:beta")]
    boards = {"beta": _board("project-beta")}
    first = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    transition = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="handoff-1",
            operation="record_handoff",
            target_status="blocked",
            result_execution_status=WorkItemExecutionStatus.HANDED_OFF,
        )
    )
    assert await get_work_item_transition_by_request("handoff-1") == transition
    await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.HANDED_OFF,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )
    issue = client.issues_by_state["state-progress"].pop()
    issue["state"] = _state("human_approved")
    issue["updatedAt"] = "2026-07-19T09:00:00+00:00"
    client.issues_by_state["state-approved"].append(issue)

    second = await reconcile_linear_decision_inbox(
        client,
        workspaces,
        boards,
        now=datetime(2026, 7, 19, 9, 5, tzinfo=UTC),
    )

    assert len(second) == 1
    assert second[0].id != first[0].id
    current = await get_active_work_item_execution("issue-execute")
    assert current is not None
    assert current.attempt == 2


async def test_reconcile_ignores_issues_without_a_project() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"].insert(
        0,
        {
            **_issue(
                "issue-no-project",
                "SYN-0",
                "Not assigned to a project",
                "human_approved",
                "unused",
            ),
            "project": None,
        },
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert [task.group_folder for task in created] == ["beta"]


async def test_reconcile_paginates_through_large_authorized_backlog() -> None:
    client = _PagedDecisionClient()
    client.issues_by_state["state-approved"] = [
        _issue(
            f"issue-{number}",
            f"SYN-{number}",
            f"Execute item {number}",
            "human_approved",
            "project-alpha",
        )
        for number in range(1, 62)
    ]

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("alpha", "Alpha", "linear:alpha")],
        {"alpha": _board("project-alpha")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert len(created) == 61
