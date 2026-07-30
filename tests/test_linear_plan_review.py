"""Checks for the hidden Linear plan-freshness reviewer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import make_container_runtime_operations

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.config.api import read_prompt
from pynchy.host.orchestrator.concurrency import GroupQueue, QueuePolicy
from pynchy.host.orchestrator.linear_plan_review import review_linear_plan
from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.workspace.api import WorkspaceProfile


@dataclass
class _Deps:
    queue: GroupQueue = field(
        default_factory=lambda: GroupQueue(
            QueuePolicy(max_concurrent=1, max_retries=0, retry_base_seconds=0),
            make_container_runtime_operations(),
        )
    )
    workspaces: dict[str, WorkspaceProfile] = field(
        default_factory=lambda: {
            "linear:project": WorkspaceProfile(
                jid="linear:project",
                name="Project",
                folder="project",
                trigger="@Pynchy",
            )
        }
    )
    reviewer_group: WorkspaceProfile | None = None
    input_source: str | None = None
    system_notices: list[str] | None = None
    review_prompt: str | None = None
    agent_result: str = "success"
    agent_output: str | None = (
        '{"decision":"amend","reason":"The helper moved","plan":"Use the renamed helper."}'
    )

    async def run_agent(self, group, _chat_jid, messages, on_output, **kwargs) -> str:
        self.reviewer_group = group
        self.input_source = kwargs["input_source"]
        self.system_notices = kwargs["extra_system_notices"]
        self.review_prompt = messages[0]["content"]
        if self.agent_output is not None:
            await on_output(ContainerOutput(status="success", result=self.agent_output))
        return self.agent_result


async def test_hidden_reviewer_returns_amended_plan_without_visible_runtime() -> None:
    deps = _Deps()

    result = await review_linear_plan(
        deps,
        LinearPlanReviewRequest(
            workspace="project",
            issue_id="issue-1",
            identifier="SYN-1",
            title="Refresh the plan",
            url="https://linear.app/example/issue/SYN-1",
            description="Approved plan",
            updated_at="2026-07-26T20:00:00Z",
            public_source=True,
        ),
        read_prompt(
            "reviewers/plan-freshness",
            Path(__file__).parents[1],
        ),
    )

    assert result.decision is LinearPlanReviewDecision.AMEND
    assert result.plan == "Use the renamed helper."
    assert deps.reviewer_group is not None
    assert deps.reviewer_group.jid == "linear-plan-review:issue-1"
    assert deps.input_source == "external:hidden_plan_review"
    assert deps.system_notices is None
    assert deps.review_prompt is not None
    normalized_prompt = " ".join(deps.review_prompt.split())
    assert "do not delegate to subagents" in normalized_prompt
    assert "HEAD or SHA movement alone is not evidence" in normalized_prompt
    assert "implementation worker owns those adaptations" in normalized_prompt
    assert "not reasons for another human approval cycle" in normalized_prompt
    assert "Return amend with a complete updated plan" in normalized_prompt
    assert "host applies that amendment and proceeds" in normalized_prompt
    assert "decision that requires human approval" in normalized_prompt
    assert "Escalate major product or technical tradeoffs back to the human" in normalized_prompt
    assert "do the planning now" in normalized_prompt
    assert "Never return instructions to rerun this review" in normalized_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_output", "expected_decision", "expected_plan"),
    [
        pytest.param(
            '```json\n{"decision":"proceed","reason":"Still current"}\n```',
            LinearPlanReviewDecision.PROCEED,
            None,
            id="fenced-json",
        ),
        pytest.param("[]", LinearPlanReviewDecision.ERROR, None, id="non-object"),
        pytest.param(
            '{"decision":"proceed"}', LinearPlanReviewDecision.ERROR, None, id="missing-reason"
        ),
        pytest.param(
            '{"decision":"proceed","reason":"ok","plan":42}',
            LinearPlanReviewDecision.ERROR,
            None,
            id="non-string-plan",
        ),
        pytest.param(
            '{"decision":"not-a-decision","reason":"ok"}',
            LinearPlanReviewDecision.ERROR,
            None,
            id="unknown-decision",
        ),
    ],
)
async def test_hidden_reviewer_fail_closed_for_invalid_agent_decisions(
    agent_output: str,
    expected_decision: LinearPlanReviewDecision,
    expected_plan: str | None,
) -> None:
    deps = _Deps(agent_output=agent_output)

    result = await review_linear_plan(
        deps,
        LinearPlanReviewRequest(
            workspace="project",
            issue_id="issue-invalid-output",
            identifier="SYN-2",
            title="Review output",
            url="https://linear.app/example/issue/SYN-2",
            description="Approved plan",
            updated_at="2026-07-26T20:00:00Z",
            public_source=False,
        ),
        "Review this plan.",
    )

    assert result.decision is expected_decision
    assert result.plan == expected_plan


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_result", ["failed", "success"])
async def test_hidden_reviewer_returns_error_when_agent_has_no_valid_result(
    agent_result: str,
) -> None:
    deps = _Deps(agent_result=agent_result, agent_output=None)

    result = await review_linear_plan(
        deps,
        LinearPlanReviewRequest(
            workspace="project",
            issue_id="issue-no-output",
            identifier="SYN-3",
            title="Review output",
            url="https://linear.app/example/issue/SYN-3",
            description="Approved plan",
            updated_at="2026-07-26T20:00:00Z",
            public_source=False,
        ),
        "Review this plan.",
    )

    assert result.decision is LinearPlanReviewDecision.ERROR
    assert result.reason.startswith("RuntimeError:")


@pytest.mark.asyncio
async def test_hidden_reviewer_returns_error_for_unknown_workspace() -> None:
    deps = _Deps()
    request = LinearPlanReviewRequest(
        workspace="missing",
        issue_id="issue-missing-workspace",
        identifier="SYN-4",
        title="Review output",
        url="https://linear.app/example/issue/SYN-4",
        description="Approved plan",
        updated_at="2026-07-26T20:00:00Z",
        public_source=False,
    )

    result = await review_linear_plan(deps, request, "Review this plan.")

    assert result == LinearPlanReviewResult(
        decision=LinearPlanReviewDecision.ERROR,
        reason="Plan reviewer could not resolve the owning workspace",
    )
