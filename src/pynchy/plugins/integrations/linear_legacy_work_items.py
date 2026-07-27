"""Migration bridge for approved work from the pre-lease Linear lifecycle."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pynchy.logger import logger
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001, RUF100 - beartype resolves migration annotations.
    LinearWorkspaceBoard,
    WorkspaceLike,
)
from pynchy.state.api import (
    WorkItemClaimConflictError,
    WorkItemClaimRequest,
    create_work_item_claim,
    get_active_work_item_execution,
    get_all_tasks,
    get_work_item_execution,
    get_work_item_transition_by_request,
    resolve_work_item_transition,
)
from pynchy.types import WorkItemExecution, WorkItemExecutionStatus, WorkItemTransitionStatus

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
            for task in await get_all_tasks()
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
    transition = await get_work_item_transition_by_request(request_id)
    if transition is None:
        try:
            await create_work_item_claim(
                WorkItemClaimRequest(
                    workspace=request.workspace.folder,
                    issue=provider_issue,
                    turn_id=None,
                    task_id=None,
                    request_id=request_id,
                    initiated_by=f"linear-legacy-task:{legacy_task_id}",
                )
            )
        except WorkItemClaimConflictError:
            current_execution = await get_active_work_item_execution(request.issue.id)
            if current_execution is None or current_execution.workspace != request.workspace.folder:
                raise
        transition = await get_work_item_transition_by_request(request_id)
    else:
        prior_execution = await get_work_item_execution(transition.execution_id)
        if prior_execution is None:
            raise RuntimeError("Legacy Linear lease transition lost its execution")
    if transition is None:
        raise RuntimeError("Legacy Linear lease transition was not persisted")

    adopted = await resolve_work_item_transition(
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
