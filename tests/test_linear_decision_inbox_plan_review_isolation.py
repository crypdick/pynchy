"""Plan-review isolation tests for the managed Linear decision inbox."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from pynchy.host.orchestrator.app import PynchyApp
from pynchy.linear_plan_types import LinearPlanReviewAdmission
from pynchy.plugins.integrations.linear_decision_inbox import (
    process_linear_plan_review_admission,
    reconcile_linear_decision_inbox,
)
from pynchy.state import get_active_work_item_execution
from tests.linear_decision_inbox_support import (
    _board,
    _DecisionClient,
    _issue,
    _Workspace,
)

pytest_plugins = ("tests.linear_decision_inbox_support",)


async def test_app_accepts_runtime_linear_plan_review_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = PynchyApp()
    task = object()
    admit = AsyncMock(return_value=task)
    start = AsyncMock()
    monkeypatch.setattr("pynchy.host.orchestrator.app.admit_linear_plan_review", admit)
    monkeypatch.setattr(
        "pynchy.host.orchestrator.app.temporal_scheduler.start_scheduled_agent_task_workflow",
        start,
    )
    admission = LinearPlanReviewAdmission(
        workspace="beta",
        issue_id="issue-execute",
        identifier="SYN-3",
        updated_at="2026-07-28T12:00:00Z",
        public_source=True,
    )

    assert await app.process_linear_plan_review_admission(admission) is True
    admit.assert_awaited_once()
    start.assert_awaited_once_with(task)


async def test_planned_work_is_deferred_by_issue_revision_without_inline_review() -> None:
    client = _DecisionClient()
    client.issues_by_state["state-approved"][0]["description"] = (
        "<!-- pynchy.plan:start -->\n"
        "## Pynchy implementation plan\n\n"
        "Implement the approved change.\n"
        "<!-- pynchy.plan:end -->"
    )
    reviewer = AsyncMock()
    defer = AsyncMock()
    client.issues_by_state["state-approved"].append(
        _issue(
            "issue-execute-2",
            "SYN-5",
            "Execute another approved task",
            "human_approved",
            "project-beta",
            description=client.issues_by_state["state-approved"][0]["description"],
        )
    )

    created = await reconcile_linear_decision_inbox(
        client,
        [_Workspace("beta", "Beta", "linear:beta")],
        {"beta": _board("project-beta")},
        review_plan=reviewer,
        defer_plan_review=defer,
    )

    assert created == []
    reviewer.assert_not_awaited()
    assert [queued.args[0].identifier for queued in defer.await_args_list] == ["SYN-3", "SYN-5"]
    assert await get_active_work_item_execution("issue-execute") is None


async def test_deferred_review_skips_a_superseded_provider_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    issue = client.issues_by_state["state-approved"][0]
    issue["description"] = "<!-- pynchy.plan:start -->approved<!-- pynchy.plan:end -->"
    issue["updatedAt"] = "2026-07-28T12:01:00Z"
    reviewer = AsyncMock()

    @asynccontextmanager
    async def client_context(**_kwargs):
        yield client

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_client",
        client_context,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.workspace_issue",
        AsyncMock(return_value=(issue, _board("project-beta"))),
    )

    task = await process_linear_plan_review_admission(
        LinearPlanReviewAdmission(
            workspace="beta",
            issue_id="issue-execute",
            identifier="SYN-3",
            updated_at="2026-07-28T12:00:00Z",
            public_source=True,
        ),
        [_Workspace("beta", "Beta", "linear:beta")],
        review_plan=reviewer,
        broadcast_host_message=AsyncMock(),
    )

    assert task is None
    reviewer.assert_not_awaited()


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ([], TypeError),
        ({"workspace": "beta"}, ValueError),
        (
            {
                "workspace": "beta",
                "issue_id": "issue-1",
                "identifier": "SYN-1",
                "updated_at": "2026-07-28T12:00:00Z",
                "public_source": "yes",
            },
            TypeError,
        ),
    ],
)
def test_plan_review_admission_rejects_malformed_temporal_payloads(
    payload: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        LinearPlanReviewAdmission.from_payload(payload)
