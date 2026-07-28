"""Checks for the hidden Linear plan-freshness reviewer."""

from __future__ import annotations

from dataclasses import dataclass, field

from conftest import make_container_runtime_operations

from pynchy.host.orchestrator.concurrency import GroupQueue, QueuePolicy
from pynchy.host.orchestrator.linear_plan_review import review_linear_plan
from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewRequest,
)
from pynchy.types import (
    ContainerOutput,
    WorkspaceProfile,
)


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

    async def run_agent(self, group, _chat_jid, _messages, on_output, **kwargs) -> str:
        self.reviewer_group = group
        self.input_source = kwargs["input_source"]
        await on_output(
            ContainerOutput(
                status="success",
                result=(
                    '{"decision":"replan","reason":"The old module is gone",'
                    '"plan":"Use the current module."}'
                ),
            )
        )
        return "success"


async def test_hidden_reviewer_returns_replacement_plan_without_visible_runtime() -> None:
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

    assert result.decision is LinearPlanReviewDecision.REPLAN
    assert result.plan == "Use the current module."
    assert deps.reviewer_group is not None
    assert deps.reviewer_group.jid == "linear-plan-review:issue-1"
    assert deps.input_source == "external:hidden_plan_review"
