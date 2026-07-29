"""Checks for the hidden Linear plan-freshness reviewer."""

from __future__ import annotations

from dataclasses import dataclass, field

from conftest import make_container_runtime_operations

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.orchestrator.concurrency import GroupQueue, QueuePolicy
from pynchy.host.orchestrator.linear_plan_review import review_linear_plan
from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewRequest,
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
    review_prompt: str | None = None

    async def run_agent(self, group, _chat_jid, messages, on_output, **kwargs) -> str:
        self.reviewer_group = group
        self.input_source = kwargs["input_source"]
        self.review_prompt = messages[0]["content"]
        await on_output(
            ContainerOutput(
                status="success",
                result=(
                    '{"decision":"amend","reason":"The helper moved",'
                    '"plan":"Use the renamed helper."}'
                ),
            )
        )
        return "success"


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
    )

    assert result.decision is LinearPlanReviewDecision.AMEND
    assert result.plan == "Use the renamed helper."
    assert deps.reviewer_group is not None
    assert deps.reviewer_group.jid == "linear-plan-review:issue-1"
    assert deps.input_source == "external:hidden_plan_review"
    assert deps.review_prompt is not None
    normalized_prompt = " ".join(deps.review_prompt.split())
    assert "HEAD or SHA movement alone is not evidence" in normalized_prompt
    assert "implementation worker owns those adaptations" in normalized_prompt
    assert "not reasons for another human approval cycle" in normalized_prompt
    assert "Return amend with a complete updated plan" in normalized_prompt
    assert "host applies that amendment and proceeds" in normalized_prompt
    assert "decision that requires human approval" in normalized_prompt
    assert "Escalate major product or technical tradeoffs back to the human" in normalized_prompt
    assert "do the planning now" in normalized_prompt
    assert "Never return instructions to rerun this review" in normalized_prompt
