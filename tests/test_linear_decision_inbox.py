"""Behavioral tests for host-leased Linear execution admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.conversation.models import ConversationSubjectKey
from pynchy.identifiers import GroupFolder
from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewResult,
)
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearClient
from pynchy.plugins.integrations.linear_decision_inbox import (
    reconcile_linear_decision_inbox,
)
from pynchy.plugins.integrations.linear_work_item_provider import (
    WorkItemLeaseRequest,
    acquire_work_item_lease,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
    TaskRunLog,
)
from pynchy.state import (
    WorkItemTransitionRequest,
    begin_in_flight_turn,
    begin_work_item_transition,
    create_task,
    get_active_work_item_execution,
    get_all_tasks,
    get_conversation_for_subject_key,
    get_in_flight_turn_for_task,
    get_task_by_id,
    get_work_item_execution_for_task,
    get_work_item_transition_by_request,
    init_test_database,
    log_task_run,
    record_task_completion,
    resolve_work_item_transition,
    update_task,
)
from pynchy.work_items.api import (
    WorkItemExecutionStatus,
    WorkItemTransitionStatus,
)


@dataclass(frozen=True)
class _Workspace:
    folder: str
    name: str
    jid: str


@dataclass(frozen=True)
class _LinearAccount:
    name: str


@dataclass(frozen=True)
class _ControlBinding:
    thread_jid: str
    closed: bool


def _state(status: str) -> dict[str, str]:
    states = {
        "ready_for_planning": {
            "id": "state-ready",
            "name": "Ready for Planning",
            "type": "unstarted",
        },
        "awaiting_plan_approval": {
            "id": "state-awaiting-plan",
            "name": "Awaiting Plan Approval",
            "type": "unstarted",
        },
        "human_approved": {
            "id": "state-approved",
            "name": "Human Approved",
            "type": "unstarted",
        },
        "in_progress": {
            "id": "state-progress",
            "name": "In Progress",
            "type": "started",
        },
        "follow_ups": {
            "id": "state-follow-ups",
            "name": "Follow-ups",
            "type": "started",
        },
    }
    return states[status]


def _issue(
    issue_id: str,
    identifier: str,
    title: str,
    status: str,
    project_id: str,
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": issue_id,
        "identifier": identifier,
        "title": title,
        "description": description,
        "url": f"https://linear.app/example/issue/{identifier}",
        "updatedAt": "2026-07-19T08:00:00+00:00",
        "state": _state(status),
        "project": {"id": project_id, "name": project_id},
    }


class _DecisionClient(LinearClient):
    team_key = "SYN"

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.issues_by_state = {
            "state-ready": [],
            "state-awaiting-plan": [],
            "state-approved": [
                _issue(
                    "issue-execute",
                    "SYN-3",
                    "Execute an approved task",
                    "human_approved",
                    "project-beta",
                ),
                _issue(
                    "issue-unmanaged",
                    "SYN-4",
                    "Ignore an unrelated project",
                    "human_approved",
                    "project-unmanaged",
                ),
            ],
            "state-progress": [],
            "state-follow-ups": [],
        }

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        return next(
            (
                issue
                for issues in self.issues_by_state.values()
                for issue in issues
                if issue["id"] == issue_id
            ),
            None,
        )

    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        if "PynchyLinearDecisionInbox" in query:
            issues = self.issues_by_state[str(variables["state_id"])]
            return {
                "workflowState": {
                    "issues": {
                        "nodes": issues,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        if "CreateComment" in query:
            issue_id = str(variables["issue_id"])
            body = str(variables["body"])
            self.comments.append((issue_id, body))
            return {
                "commentCreate": {
                    "success": True,
                    "comment": {
                        "id": f"comment-{len(self.comments)}",
                        "body": body,
                        "createdAt": "2026-07-19T08:01:00+00:00",
                        "updatedAt": "2026-07-19T08:01:00+00:00",
                        "issue": {"id": issue_id},
                    },
                }
            }
        issue = await self.get_issue(str(variables["issue_id"]))
        if issue is None:
            raise AssertionError("test update targeted an unknown issue")
        old_state_id = str(issue["state"]["id"])
        new_state_id = str(variables["state_id"])
        self.issues_by_state[old_state_id].remove(issue)
        issue["state"] = next(
            state
            for state in (
                _state("awaiting_plan_approval"),
                _state("human_approved"),
                _state("in_progress"),
                _state("follow_ups"),
            )
            if state["id"] == new_state_id
        )
        if "description" in variables:
            issue["description"] = str(variables["description"])
        self.issues_by_state[new_state_id].append(issue)
        return {"issueUpdate": {"success": True, "issue": issue}}


class _PagedDecisionClient(_DecisionClient):
    async def query(self, query: str, **variables: object) -> dict[str, Any]:
        if "PynchyLinearDecisionInbox" not in query:
            return await super().query(query, **variables)
        issues = self.issues_by_state[str(variables["state_id"])]
        start = int(str(variables["after"] or 0))
        end = start + 50
        has_next = end < len(issues)
        return {
            "workflowState": {
                "issues": {
                    "nodes": issues[start:end],
                    "pageInfo": {
                        "hasNextPage": has_next,
                        "endCursor": str(end) if has_next else None,
                    },
                }
            }
        }


def _board(project_id: str) -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": project_id},
        states={
            "ready_for_planning": _state("ready_for_planning"),
            "awaiting_plan_approval": _state("awaiting_plan_approval"),
            "human_approved": _state("human_approved"),
            "in_progress": _state("in_progress"),
            "follow_ups": _state("follow_ups"),
        },
    )


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


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
    assert "Objective:" in task.prompt
    assert "Authority:" in task.prompt
    assert "Success:" in task.prompt
    assert "linear_claim_work_item" not in task.prompt
    assert len(await get_all_tasks()) == 1


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


async def test_plan_review_error_returns_issue_to_awaiting_approval() -> None:
    client = _DecisionClient()
    issue = client.issues_by_state["state-approved"][0]
    issue["description"] = (
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Implement the approved change.\n"
        "<!-- pynchy.plan:end -->"
    )
    reviewer = AsyncMock(
        return_value=LinearPlanReviewResult(
            LinearPlanReviewDecision.ERROR,
            "RuntimeError: plan reviewer failed",
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
    assert client.comments[0][0] == "issue-execute"
    assert "RuntimeError: plan reviewer failed" in client.comments[0][1]


async def test_missing_plan_reviewer_returns_issue_to_awaiting_approval() -> None:
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
    assert issue["state"]["id"] == "state-awaiting-plan"
    assert "Plan reviewer is unavailable" in client.comments[0][1]


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
    assert "linear_submit_plan" in task.prompt
    assert "Awaiting Plan Approval" in task.prompt
    assert "supplies the user's scope and stated facts" in task.prompt
    assert "without asking the user to reconfirm" in task.prompt
    assert "not a consent request" in task.prompt
    assert "Do not pad the plan with generic confirmation or permission steps" in task.prompt
    assert "Do not execute" in task.prompt
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
    assert "linear_submit_plan" in task.prompt


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
    assert "EXTERNAL_UNTRUSTED_CONTENT" not in task.prompt


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
    assert "preserve useful logs before teardown" in follow_up_tasks[0].prompt
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


async def test_reconcile_repairs_webhook_lease_that_has_no_durable_task() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = [
        _issue(
            "issue-webhook",
            "SYN-13",
            "Resume webhook work",
            "human_approved",
            "project-beta",
        )
    ]
    board = _board("project-beta")
    execution = await acquire_work_item_lease(
        client,
        WorkItemLeaseRequest(
            workspace="beta",
            issue_id="issue-webhook",
            request_id="linear-webhook:delivery-1:lease",
            initiated_by="linear-webhook:delivery-1",
            board=board,
        ),
    )
    assert execution.task_id is None

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": board},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert len(created) == 1
    assert created[0].id.startswith("linear-execute-syn-13-")
    repaired = await get_active_work_item_execution("issue-webhook")
    assert repaired is not None
    assert repaired.task_id == created[0].id
    assert repaired.temporal_workflow_id is not None


async def test_reconcile_does_not_infer_authority_from_unleased_in_progress() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = []
    client.issues_by_state["state-progress"] = [
        _issue(
            "issue-unleased",
            "SYN-99",
            "Do not infer approval",
            "in_progress",
            "project-beta",
        )
    ]

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 19, 8, 5, tzinfo=UTC),
    )

    assert created == []
    assert await get_active_work_item_execution("issue-unleased") is None


async def test_reconcile_adopts_completed_legacy_plan_as_approval_evidence() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"] = []
    client.issues_by_state["state-progress"] = [
        _issue(
            "issue-legacy",
            "SYN-13",
            "Resume legacy approved work",
            "in_progress",
            "project-beta",
        )
    ]
    await create_task(
        ScheduledTask(
            id="linear-ready-for-planning-syn-13-wrong-issue",
            group_folder="retired-pynchy-dev",
            chat_jid="linear:retired-pynchy-dev",
            prompt=(
                "[Source: linear-decision-inbox]\n"
                '{"identifier": "SYN-13", "issue_id": "another-issue"}'
            ),
            schedule_type="once",
            schedule_value="2026-07-19T08:00:00+00:00",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            status="completed",
            created_at="2026-07-19T08:00:00+00:00",
        )
    )
    await create_task(
        ScheduledTask(
            id="linear-ready-for-planning-syn-13-proof",
            group_folder="retired-pynchy-dev",
            chat_jid="linear:retired-pynchy-dev",
            prompt=(
                "[Source: linear-decision-inbox]\n"
                '{"identifier": "SYN-13", "issue_id": "issue-legacy"}'
            ),
            schedule_type="once",
            schedule_value="2026-07-19T08:00:00+00:00",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            status="completed",
            created_at="2026-07-19T08:00:00+00:00",
        )
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        now=datetime(2026, 7, 25, 8, 5, tzinfo=UTC),
    )

    assert len(created) == 1
    assert created[0].id.startswith("linear-execute-syn-13-")
    adopted = await get_active_work_item_execution("issue-legacy")
    assert adopted is not None
    assert adopted.initiated_by == ("linear-legacy-task:linear-ready-for-planning-syn-13-proof")
    assert adopted.task_id == created[0].id


async def test_reconcile_reactivates_quiet_completed_task_after_grace_period() -> None:
    client = _DecisionClient()
    workspace = _Workspace("beta", "Beta", "linear:beta")
    board = _board("project-beta")
    observed_at = datetime.now(UTC)
    created = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at,
    )
    task = created[0]
    await log_task_run(
        TaskRunLog(
            task_id=task.id,
            run_at=observed_at.isoformat(),
            duration_ms=1,
            status="success",
        )
    )
    await record_task_completion(task.id, last_result="Stopped without transition", completed=True)

    recovered = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at + timedelta(minutes=6),
    )

    assert [item.id for item in recovered] == [task.id]
    active = await get_task_by_id(task.id)
    assert active is not None
    assert active.status == "active"
    assert active.schedule_value == (observed_at + timedelta(minutes=6)).isoformat()


async def test_reconcile_reactivates_moved_active_execution_in_original_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_tasks.linear_account_for_workspace",
        lambda _workspace: _LinearAccount(name="linear"),
    )
    original = _Workspace("beta", "Beta", "linear:beta")
    destination = _Workspace("alpha", "Alpha", "linear:alpha")
    boards = {
        original.folder: _board("project-beta"),
        destination.folder: _board("project-alpha"),
    }
    observed_at = datetime.now(UTC)
    task = (
        await reconcile_linear_decision_inbox(
            client,
            [destination, original],
            boards,
            now=observed_at,
        )
    )[0]
    assert task.conversation_id is not None
    await record_task_completion(task.id, last_result="Stopped without transition", completed=True)

    issue = client.issues_by_state["state-progress"][0]
    issue["project"] = {"id": "project-alpha", "name": "project-alpha"}

    recovered = await reconcile_linear_decision_inbox(
        client,
        [destination, original],
        boards,
        now=observed_at + timedelta(minutes=6),
    )

    assert [item.id for item in recovered] == [task.id]
    assert recovered[0].group_folder == original.folder
    assert recovered[0].chat_jid == original.jid
    assert recovered[0].conversation_id == task.conversation_id
    active_task = await get_task_by_id(task.id)
    assert active_task is not None
    assert active_task.status == "active"
    assert active_task.group_folder == original.folder
    assert active_task.chat_jid == original.jid
    assert active_task.conversation_id == task.conversation_id
    assert len(await get_all_tasks()) == 1
    assert (
        await get_conversation_for_subject_key(
            ConversationSubjectKey("issue-execute"),
            workspace=GroupFolder(destination.folder),
            namespace_suffix=":issue",
        )
        is None
    )
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    assert execution.workspace == original.folder
    assert execution.task_id == task.id


async def test_reconcile_resumes_paused_execution_after_grace_and_clears_terminal_turn() -> None:
    client = _DecisionClient()
    workspace = _Workspace("beta", "Beta", "linear:beta")
    board = _board("project-beta")
    observed_at = datetime.now(UTC)
    task = (
        await reconcile_linear_decision_inbox(
            client,
            [workspace],
            {"beta": board},
            now=observed_at,
        )
    )[0]
    await record_task_completion(task.id, last_result="Repeated terminal failure", completed=False)
    await update_task(task.id, {"status": "paused"})
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="terminal-scheduled-turn",
            chat_jid=task.chat_jid,
            group_folder=task.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at=observed_at.isoformat(),
            task_id=task.id,
        )
    )

    assert (
        await reconcile_linear_decision_inbox(
            client,
            [workspace],
            {"beta": board},
            now=observed_at + timedelta(minutes=1),
        )
        == []
    )
    paused = await get_task_by_id(task.id)
    assert paused is not None
    assert paused.status == "paused"
    assert paused.occurrence_generation == 0
    assert await get_in_flight_turn_for_task(task.id) is not None

    resumed = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at + timedelta(minutes=6),
    )

    assert [item.id for item in resumed] == [task.id]
    active = await get_task_by_id(task.id)
    assert active is not None
    assert active.status == "active"
    assert active.occurrence_generation == 1
    assert active.superseded_occurrence_generation == 0
    assert await get_in_flight_turn_for_task(task.id) is None
    execution = await get_active_work_item_execution("issue-execute")
    assert execution is not None
    assert execution.temporal_workflow_id is not None
    assert execution.temporal_workflow_id.endswith("-resume-1")


async def test_reconcile_bounds_incomplete_outcome_recovery() -> None:
    client = _DecisionClient()
    workspace = _Workspace("beta", "Beta", "linear:beta")
    board = _board("project-beta")
    observed_at = datetime.now(UTC)
    created = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at,
    )
    task = created[0]
    for attempt in range(3):
        await log_task_run(
            TaskRunLog(
                task_id=task.id,
                run_at=(observed_at + timedelta(seconds=attempt)).isoformat(),
                duration_ms=1,
                status="incomplete",
            )
        )
    await record_task_completion(
        task.id,
        last_result="Incomplete: no Linear lifecycle outcome",
        completed=True,
    )

    recovered = await reconcile_linear_decision_inbox(
        client,
        [workspace],
        {"beta": board},
        now=observed_at + timedelta(minutes=6),
    )

    assert recovered == []
    bounded = await get_task_by_id(task.id)
    assert bounded is not None
    assert bounded.status == "completed"
