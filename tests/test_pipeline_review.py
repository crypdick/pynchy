"""Tests for configured pipeline reviewer isolation."""

from __future__ import annotations

from typing import Any

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.orchestrator.pipeline_review import (
    PipelineReviewRequest,
    run_pipeline_reviews,
)
from pynchy.host.orchestrator.workspace_config import clear_runtime_workspace_restrictions


class _Queue:
    async def run_serialized_task(self, _target, _task_id, run):
        return await run()


class _Deps:
    def __init__(self) -> None:
        self.queue = _Queue()
        self.calls: list[tuple[str, str]] = []

    async def run_agent(
        self,
        group,
        _chat_jid,
        _messages: list[dict[str, Any]],
        on_output=None,
        extra_system_notices=None,
        *,
        input_source: str,
        **_kwargs,
    ) -> str:
        _ = extra_system_notices
        self.calls.append((group.folder, input_source))
        await on_output(ContainerOutput(status="success", result=f"review from {input_source}"))
        return "success"


async def test_pipeline_reviewers_run_in_separate_contexts() -> None:
    clear_runtime_workspace_restrictions()
    deps = _Deps()
    try:
        results = await run_pipeline_reviews(
            deps,
            PipelineReviewRequest(
                parent_workspace="alpha",
                task_id="task-1",
                task_prompt="Do the work",
                executor_result="Done",
                reviewer_ids=("reviewers/security", "reviewers/docs"),
                repo_access=None,
            ),
        )
    finally:
        clear_runtime_workspace_restrictions()

    assert [result.reviewer for result in results] == [
        "reviewers/security",
        "reviewers/docs",
    ]
    assert deps.calls[0][0] != deps.calls[1][0]
    assert [source for _, source in deps.calls] == [
        "hidden:pipeline-review:reviewers/security",
        "hidden:pipeline-review:reviewers/docs",
    ]
