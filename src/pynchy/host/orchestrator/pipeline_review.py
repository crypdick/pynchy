"""Independent reviewer execution for configured pipeline stages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import (  # noqa: TC003 - beartype resolves annotations at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.agent_protocol.api import ContainerOutput  # noqa: TC001 - see above.
from pynchy.host.orchestrator.pipeline_context import reviewer_ids_for_context
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspacePolicy,
    get_settings,
    load_resolved_config,
    register_runtime_workspace_policy,
)
from pynchy.logger import logger
from pynchy.scheduling.api import (  # noqa: TC001 - beartype resolves scheduler annotations.
    ScheduledTask,
)
from pynchy.workspace.api import (
    CapabilityRule,
    RuntimeTarget,
    WorkspaceProfile,
)


@runtime_checkable
class PipelineReviewQueue(Protocol):
    async def run_serialized_task(
        self,
        target: RuntimeTarget,
        task_id: str,
        run: Callable[[], Awaitable[str]],
    ) -> str: ...


@runtime_checkable
class PipelineReviewDeps(Protocol):
    @property
    def queue(self) -> PipelineReviewQueue: ...

    async def run_agent(  # noqa: PLR0913 - mirrors the orchestrator contract.
        self,
        group: WorkspaceProfile,
        chat_jid: str,
        messages: list[dict[str, Any]],
        on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
        extra_system_notices: list[str] | None = None,
        *,
        is_scheduled_task: bool = False,
        repo_access_override: str | None = None,
        input_source: str = "user",
    ) -> str: ...


@runtime_checkable
class PipelineReviewHostDeps(PipelineReviewDeps, Protocol):
    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...


@dataclass(frozen=True)
class PipelineReviewResult:
    reviewer: str
    review: str


@dataclass(frozen=True)
class PipelineReviewRequest:
    parent_workspace: str
    task_id: str
    task_prompt: str
    executor_result: str | None
    reviewer_ids: tuple[str, ...]
    repo_access: str | None


def _reviewer_profile(
    parent_workspace: str,
    task_id: str,
    reviewer: str,
) -> WorkspaceProfile:
    digest = hashlib.sha256(f"{task_id}\0{reviewer}".encode()).hexdigest()[:16]
    folder = f"pipeline-review-{digest}"
    register_runtime_workspace_policy(
        folder,
        RuntimeWorkspacePolicy(
            parent_workspace=parent_workspace,
            tools=(),
            capabilities={"*": CapabilityRule(decision="deny")},
        ),
    )
    return WorkspaceProfile(
        jid=f"pipeline-review:{digest}",
        name=f"{reviewer.removeprefix('reviewers/')} Reviewer",
        folder=folder,
        trigger="",
        is_admin=False,
    )


async def _run_reviewer(
    deps: PipelineReviewDeps,
    request: PipelineReviewRequest,
    reviewer: str,
) -> PipelineReviewResult:
    group = _reviewer_profile(request.parent_workspace, request.task_id, reviewer)
    final_results: list[str] = []

    async def on_output(  # noqa: RUF029 - run_agent requires an async callback.
        output: ContainerOutput,
    ) -> None:
        if output.type == "result" and output.result:
            final_results.append(output.result)

    async def run_review() -> str:
        payload = json.dumps(
            {
                "task": request.task_prompt,
                "executor_result": request.executor_result,
            },
            ensure_ascii=False,
            indent=2,
        )
        result = await deps.run_agent(
            group,
            group.jid,
            [{"role": "user", "content": payload}],
            on_output=on_output,
            extra_system_notices=None,
            is_scheduled_task=True,
            repo_access_override=request.repo_access,
            input_source=f"hidden:pipeline-review:{reviewer}",
        )
        if result != "success" or not final_results:
            raise RuntimeError(f"Pipeline reviewer {reviewer!r} did not return a review")
        return final_results[-1]

    review = await deps.queue.run_serialized_task(
        RuntimeTarget.from_workspace(group),
        f"{group.folder}-{hashlib.sha256(request.task_prompt.encode()).hexdigest()[:16]}",
        run_review,
    )
    return PipelineReviewResult(reviewer=reviewer, review=review)


async def run_pipeline_reviews(
    deps: PipelineReviewDeps,
    request: PipelineReviewRequest,
) -> tuple[PipelineReviewResult, ...]:
    """Run configured reviewers in isolated contexts and return their reviews."""
    return tuple(
        [
            await _run_reviewer(
                deps,
                request,
                reviewer=reviewer,
            )
            for reviewer in request.reviewer_ids
        ]
    )


async def run_configured_pipeline_reviews(
    task: ScheduledTask,
    deps: PipelineReviewHostDeps,
    group: WorkspaceProfile,
    *,
    result: str | None,
    error: str | None,
) -> tuple[str | None, str | None]:
    """Run the reviewers selected for one successful scheduled executor."""
    if error is not None:
        return result, error
    settings = get_settings()
    reviewers = reviewer_ids_for_context(
        load_resolved_config(group.folder, settings=settings),
        task.input_source,
        settings=settings,
    )
    if not reviewers:
        return result, None
    try:
        reviews = await run_pipeline_reviews(
            deps,
            PipelineReviewRequest(
                parent_workspace=group.folder,
                task_id=task.id,
                task_prompt=task.prompt,
                executor_result=result,
                reviewer_ids=reviewers,
                repo_access=task.repo_access,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - selected reviewers fail the scheduled run closed.
        logger.exception(
            "Pipeline review failed",
            task_id=task.id,
            error_type=type(exc).__name__,
        )
        return result, f"Pipeline review failed: {type(exc).__name__}: {exc}"

    rendered = "\n\n".join(f"Reviewer {review.reviewer}:\n{review.review}" for review in reviews)
    await deps.broadcast_host_message(group.jid, rendered)
    return (f"{result}\n\n{rendered}" if result else rendered), None
