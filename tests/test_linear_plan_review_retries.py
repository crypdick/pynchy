"""Terminal behavior for Temporal-retried Linear plan reviews."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

import pynchy.host.orchestrator.temporal.linear_work_items as temporal_linear_work_items
import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
from pynchy.host.orchestrator.temporal.runtime_state import TemporalActivityInfo
from pynchy.linear_plan_types import (
    LinearPlanReviewAdmission,
    LinearPlanReviewBlockedError,
    LinearPlanReviewError,
)
from pynchy.plugins.integrations.linear_decision_inbox import (
    process_linear_plan_review_admission,
)
from tests.linear_decision_inbox_support import _board, _DecisionClient, _issue, _Workspace
from tests.temporal_scheduler_support import NullSchedulerDeps

pytest_plugins = ("tests.linear_decision_inbox_support", "tests.temporal_scheduler_support")


async def test_plan_review_attempt_is_forwarded_and_final_failure_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _DecisionClient()
    issue = client.issues_by_state["state-approved"][0]
    board = _board("project-beta")
    board.states["blocked"] = {"id": "state-blocked", "name": "Blocked", "type": "started"}
    issue_payload = _issue("issue-execute", "SYN-3", "Execute", "human_approved", "project-beta")
    admission = LinearPlanReviewAdmission(
        workspace="beta",
        issue_id="issue-execute",
        identifier="SYN-3",
        updated_at=issue["updatedAt"],
        public_source=True,
    )

    @asynccontextmanager
    async def fake_linear_client(*, workspace: str):
        assert workspace == "beta"
        yield client

    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.linear_client",
        fake_linear_client,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.workspace_issue",
        AsyncMock(return_value=(issue_payload, board)),
    )
    admit = AsyncMock(side_effect=LinearPlanReviewError("reviewer timed out"))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.admit_decision_issue", admit
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox.update_issue_state", AsyncMock()
    )
    report = AsyncMock()
    reset = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox._report_plan_review_status", report
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_decision_inbox._reset_plan_review_context", reset
    )

    with pytest.raises(LinearPlanReviewError):
        await process_linear_plan_review_admission(
            admission,
            [_Workspace("beta", "Beta", "linear:beta")],
            review_plan=AsyncMock(),
            broadcast_host_message=AsyncMock(),
            attempt=2,
            reset_context=reset,
        )
    assert admit.await_args.args[4].plan_review_attempt == 2

    admit.reset_mock(side_effect=True)
    admit.side_effect = LinearPlanReviewError("reviewer timed out")
    with pytest.raises(LinearPlanReviewBlockedError):
        await process_linear_plan_review_admission(
            admission,
            [_Workspace("beta", "Beta", "linear:beta")],
            review_plan=AsyncMock(),
            broadcast_host_message=AsyncMock(),
            attempt=3,
            reset_context=reset,
        )
    assert report.await_args.args[3] == (
        "Plan review failed after three attempts and this issue was moved to Blocked. "
        "Fix it, then move it to Human Approved to retry."
    )
    reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_temporal_final_plan_review_failure_completes_as_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deps = NullSchedulerDeps()
    deps.process_linear_plan_review_admission = AsyncMock(
        side_effect=LinearPlanReviewBlockedError("reviewer timed out")
    )

    @asynccontextmanager
    async def no_heartbeats(_activity_id):
        yield

    monkeypatch.setattr(
        temporal_linear_work_items.activity,
        "info",
        lambda: TemporalActivityInfo(workflow_id="linear-plan-review-blocked", attempt=3),
    )
    monkeypatch.setattr(temporal_linear_work_items, "activity_heartbeats", no_heartbeats)
    temporal_scheduler.reset_temporal_scheduler_status()
    temporal_scheduler.bind_scheduler_deps(deps)

    result = await temporal_linear_work_items.run_linear_plan_review_admission(
        {
            "workspace": "project",
            "issue_id": "issue-1",
            "identifier": "SYN-1",
            "updated_at": "2026-07-28T12:00:00Z",
            "public_source": True,
        }
    )

    assert result == "blocked"
    assert deps.process_linear_plan_review_admission.await_args.kwargs["attempt"] == 3
    tracked = temporal_scheduler.get_temporal_scheduler_status()["tracked_results"][
        "linear-plan-review:SYN-1"
    ]
    assert tracked["result"] == "blocked"
    assert tracked["error"] == "reviewer timed out"
