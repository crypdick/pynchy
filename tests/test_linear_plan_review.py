"""Checks for the hidden Linear plan-freshness reviewer."""

from __future__ import annotations

import json
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
            QueuePolicy(max_concurrent=1, max_retries=0, retry_base_seconds=0.0),
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
    review_prompts: list[str] = field(default_factory=list)
    reasoning_efforts: list[str | None] = field(default_factory=list)
    agent_result: str = "success"
    agent_output: str | None = (
        '{"decision":"amend","reason":"The helper moved","plan":"Use the renamed helper."}'
    )
    agent_outputs: list[str | None] | None = None
    agent_error: str | None = None

    async def run_agent(self, group, _chat_jid, messages, on_output, **kwargs) -> str:
        self.reviewer_group = group
        self.input_source = kwargs["input_source"]
        self.system_notices = kwargs["extra_system_notices"]
        self.review_prompt = messages[0]["content"]
        self.review_prompts.append(self.review_prompt)
        self.reasoning_efforts.append(kwargs["model_reasoning_effort_override"])
        output = (
            self.agent_outputs[len(self.review_prompts) - 1]
            if self.agent_outputs is not None
            else self.agent_output
        )
        if self.agent_error is not None:
            await on_output(ContainerOutput(status="error", error=self.agent_error))
        if output is not None:
            await on_output(ContainerOutput(status="success", result=output))
        return self.agent_result


def _embedded_issue(prompt: str) -> dict[str, object]:
    payload = json.loads(prompt[prompt.rfind("{") :])
    assert isinstance(payload, dict)
    return payload


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
    assert deps.reasoning_efforts == ["medium"]
    assert deps.review_prompt is not None
    issue = _embedded_issue(deps.review_prompt)
    assert issue["id"] == "issue-1"
    assert issue["identifier"] == "SYN-1"


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
        pytest.param(
            '```json\n{"decision":"proceed","reason":"Still current"}',
            LinearPlanReviewDecision.PROCEED,
            None,
            id="unterminated-fence",
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
async def test_hidden_reviewer_returns_validation_error_for_one_correction_turn() -> None:
    deps = _Deps(
        agent_outputs=[
            '{"decision":"proceed"}',
            '{"decision":"proceed","reason":"The approved plan is current."}',
        ]
    )

    result = await review_linear_plan(
        deps,
        LinearPlanReviewRequest(
            workspace="project",
            issue_id="issue-repair",
            identifier="SYN-5",
            title="Repair reviewer output",
            url="https://linear.app/example/issue/SYN-5",
            description="Approved plan",
            updated_at="2026-07-26T20:00:00Z",
            public_source=False,
        ),
        "Review this plan.",
    )

    assert result.decision is LinearPlanReviewDecision.PROCEED
    assert deps.reasoning_efforts == ["medium", "medium"]
    assert len(deps.review_prompts) == 2
    assert deps.review_prompts[1] != deps.review_prompts[0]


@pytest.mark.asyncio
async def test_hidden_reviewer_preserves_runner_error_details() -> None:
    deps = _Deps(
        agent_result="error",
        agent_output=None,
        agent_error="Host agent runner initial_progress timeout: codex startup blocked",
    )

    result = await review_linear_plan(
        deps,
        LinearPlanReviewRequest(
            workspace="project",
            issue_id="issue-runner-error",
            identifier="SYN-6",
            title="Report runner failure",
            url="https://linear.app/example/issue/SYN-6",
            description="Approved plan",
            updated_at="2026-07-26T20:00:00Z",
            public_source=False,
        ),
        "Review this plan.",
    )

    assert result.decision is LinearPlanReviewDecision.ERROR
    assert "codex startup blocked" in result.reason


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
