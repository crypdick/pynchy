"""Tests for configured pipeline reviewer isolation."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import ContainerOutput
from pynchy.host.orchestrator.pipeline_review import (
    PipelineReviewRequest,
    PipelineReviewResult,
    run_configured_pipeline_reviews,
    run_pipeline_reviews,
)
from pynchy.host.orchestrator.workspace_config import clear_runtime_workspace_policies
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.workspace.api import WorkspaceProfile


class _Queue:
    async def run_serialized_task(self, _target, _task_id, run):
        return await run()


class _Deps:
    def __init__(self) -> None:
        self.queue = _Queue()
        self.calls: list[tuple[str, str]] = []

    async def broadcast_host_message(self, _chat_jid: str, _text: str) -> None: ...

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


class _FailedDeps(_Deps):
    async def run_agent(self, *_args, **_kwargs) -> str:
        return "error"


class _EmptyThenValidDeps(_Deps):
    async def run_agent(
        self,
        _group,
        _chat_jid,
        _messages,
        on_output=None,
        **_kwargs,
    ) -> str:
        await on_output(ContainerOutput(status="success", type="result", result=""))
        await on_output(ContainerOutput(status="success", type="result", result="valid review"))
        return "success"


def _task() -> ScheduledTask:
    return ScheduledTask(
        id="task-1",
        group_folder="alpha",
        chat_jid="discord:channel:alpha",
        prompt="Do the work",
        schedule_type="once",
        schedule_value="2026-07-29T00:00:00+00:00",
        session_policy=SessionPolicy.CONTINUE,
        input_source="scheduled_task",
    )


def _group() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:channel:alpha",
        name="Alpha",
        folder="alpha",
        trigger="@Pynchy",
    )


async def test_pipeline_reviewers_run_in_separate_contexts() -> None:
    clear_runtime_workspace_policies()
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
        clear_runtime_workspace_policies()

    assert [result.reviewer for result in results] == [
        "reviewers/security",
        "reviewers/docs",
    ]
    assert deps.calls[0][0] != deps.calls[1][0]
    assert [source for _, source in deps.calls] == [
        "hidden:pipeline-review:reviewers/security",
        "hidden:pipeline-review:reviewers/docs",
    ]


async def test_pipeline_reviewer_requires_a_successful_final_result() -> None:
    clear_runtime_workspace_policies()
    try:
        with pytest.raises(RuntimeError, match="did not return a review"):
            await run_pipeline_reviews(
                _FailedDeps(),
                PipelineReviewRequest(
                    parent_workspace="alpha",
                    task_id="task-1",
                    task_prompt="Do the work",
                    executor_result="Done",
                    reviewer_ids=("reviewers/security",),
                    repo_access=None,
                ),
            )
    finally:
        clear_runtime_workspace_policies()


async def test_pipeline_reviewer_ignores_empty_result_events() -> None:
    clear_runtime_workspace_policies()
    try:
        results = await run_pipeline_reviews(
            _EmptyThenValidDeps(),
            PipelineReviewRequest(
                parent_workspace="alpha",
                task_id="task-1",
                task_prompt="Do the work",
                executor_result="Done",
                reviewer_ids=("reviewers/security",),
                repo_access=None,
            ),
        )
    finally:
        clear_runtime_workspace_policies()

    assert results[0].review == "valid review"


async def test_configured_pipeline_reviews_broadcast_and_append_successful_reviews() -> None:
    deps = _Deps()
    deps.broadcast_host_message = AsyncMock()
    review = PipelineReviewResult("reviewers/security", "Looks good")
    settings = MagicMock()

    with (
        patch("pynchy.host.orchestrator.pipeline_review.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.pipeline_review.load_resolved_config",
            return_value=MagicMock(),
        ),
        patch(
            "pynchy.host.orchestrator.pipeline_review.reviewer_ids_for_context",
            return_value=("reviewers/security",),
        ),
        patch(
            "pynchy.host.orchestrator.pipeline_review.run_pipeline_reviews",
            new_callable=AsyncMock,
            return_value=(review,),
        ),
    ):
        result = await run_configured_pipeline_reviews(
            _task(),
            deps,
            _group(),
            result="Done",
            error=None,
        )

    assert result == ("Done\n\nReviewer reviewers/security:\nLooks good", None)
    deps.broadcast_host_message.assert_awaited_once_with(
        "discord:channel:alpha", "Reviewer reviewers/security:\nLooks good"
    )


async def test_configured_pipeline_reviews_return_failure_as_task_error() -> None:
    deps = _Deps()
    settings = MagicMock()

    with (
        patch("pynchy.host.orchestrator.pipeline_review.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.pipeline_review.load_resolved_config",
            return_value=MagicMock(),
        ),
        patch(
            "pynchy.host.orchestrator.pipeline_review.reviewer_ids_for_context",
            return_value=("reviewers/security",),
        ),
        patch(
            "pynchy.host.orchestrator.pipeline_review.run_pipeline_reviews",
            new_callable=AsyncMock,
            side_effect=RuntimeError("reviewer unavailable"),
        ),
    ):
        result = await run_configured_pipeline_reviews(
            _task(),
            deps,
            _group(),
            result="Done",
            error=None,
        )

    assert result == ("Done", "Pipeline review failed: RuntimeError: reviewer unavailable")
