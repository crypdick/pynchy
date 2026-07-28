"""Translate ready-for-planning Linear transitions into work-item leases."""

from __future__ import annotations

import re
from collections.abc import (  # noqa: TC003 - beartype resolves configured transition callbacks at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001 - beartype resolves migration annotations.
    LinearWorkspaceBoard,
    WorkspaceLike,
)
from pynchy.work_items.api import (
    WorkItemClaimConflictError,
    WorkItemExecution,
    WorkItemExecutionStatus,
    WorkItemTransitionStatus,
)

_LEGACY_PLANNING_TASK_PREFIX = "linear-ready-for-planning-"


@runtime_checkable
class LegacyDecisionClient(Protocol):
    async def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        """Return one issue by its durable provider ID."""


@runtime_checkable
class LegacyDecisionIssue(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def identifier(self) -> str: ...


@dataclass(frozen=True)
class LegacyAdoptionRequest:
    client: LegacyDecisionClient
    issue: LegacyDecisionIssue
    workspace: WorkspaceLike
    board: LinearWorkspaceBoard


@dataclass(frozen=True)
class LinearLegacyWorkItemRuntime:
    """Durable operations that create work-item leases from transitions."""

    get_all_tasks: Callable[[], Awaitable[list[Any]]]
    get_transition_by_request: Callable[[str], Awaitable[Any]]
    create_claim: Callable[[Any], Awaitable[Any]]
    claim_request: Callable[..., Any]
    get_active_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    get_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    resolve_transition: Callable[..., Awaitable[WorkItemExecution]]


@dataclass
class _RuntimeState:
    runtime: LinearLegacyWorkItemRuntime | None = None


_runtime = _RuntimeState()


def configure_linear_legacy_work_item_runtime(runtime: LinearLegacyWorkItemRuntime) -> None:
    """Set the durable operations that translate planning transitions."""
    _runtime.runtime = runtime


def _configured_runtime() -> LinearLegacyWorkItemRuntime:
    if _runtime.runtime is None:
        raise RuntimeError("Linear legacy work-item runtime has not been configured")
    return _runtime.runtime


def _state_id(board: LinearWorkspaceBoard, status: str) -> str:
    state = board.states.get(status)
    state_id = state.get("id") if isinstance(state, dict) else None
    if state_id is None:
        raise ValueError(f"Linear board lacks state {status}")
    if not isinstance(state_id, str):
        raise TypeError(f"Linear board state {status} lacks a text ID")
    return state_id


async def _legacy_planning_task_id(request: LegacyAdoptionRequest) -> str | None:
    prefix = f"{_LEGACY_PLANNING_TASK_PREFIX}{request.issue.identifier.lower()}-"
    issue_id_pattern = re.compile(
        rf'"issue_id"\s*:\s*"{re.escape(request.issue.id)}"',
    )
    task = next(
        (
            task
            for task in await _configured_runtime().get_all_tasks()
            if task.id.startswith(prefix)
            and task.status == "completed"
            and "[Source: linear-decision-inbox]" in task.prompt
            and issue_id_pattern.search(task.prompt) is not None
        ),
        None,
    )
    return task.id if task is not None else None


async def adopt_legacy_in_progress_execution(
    request: LegacyAdoptionRequest,
) -> WorkItemExecution | None:
    """Adopt completed planning-task evidence into the execution lease."""
    runtime = _configured_runtime()
    legacy_task_id = await _legacy_planning_task_id(request)
    provider_issue = await request.client.get_issue(request.issue.id)
    if legacy_task_id is None or provider_issue is None:
        return None
    state = provider_issue.get("state")
    if not isinstance(state, dict) or state.get("id") != _state_id(
        request.board,
        "in_progress",
    ):
        return None

    request_id = f"linear-legacy:{legacy_task_id}:lease"
    transition = await runtime.get_transition_by_request(request_id)
    if transition is None:
        try:
            await runtime.create_claim(
                runtime.claim_request(
                    workspace=request.workspace.folder,
                    issue=provider_issue,
                    turn_id=None,
                    task_id=None,
                    request_id=request_id,
                    initiated_by=f"linear-legacy-task:{legacy_task_id}",
                )
            )
        except WorkItemClaimConflictError:
            current_execution = await runtime.get_active_execution(request.issue.id)
            if current_execution is None or current_execution.workspace != request.workspace.folder:
                raise
        transition = await runtime.get_transition_by_request(request_id)
    else:
        prior_execution = await runtime.get_execution(transition.execution_id)
        if prior_execution is None:
            raise RuntimeError("Legacy Linear lease transition lost its execution")
    if transition is None:
        raise RuntimeError("Legacy Linear lease transition was not persisted")

    adopted = await runtime.resolve_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.IN_PROGRESS,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
        issue=provider_issue,
    )
    logger.warning(
        "Adopted legacy Linear approval into an execution lease",
        issue=request.issue.identifier,
        execution_id=adopted.id,
        evidence_task_id=legacy_task_id,
    )
    return adopted
