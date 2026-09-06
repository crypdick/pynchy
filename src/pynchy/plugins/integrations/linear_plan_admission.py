"""Execution admission outcomes for approved Linear plans."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewError,
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_boards import (
    LinearWorkspaceBoard,
)
from pynchy.plugins.integrations.linear_plans import description_with_plan, update_issue_plan
from pynchy.plugins.integrations.linear_statuses import (
    AWAITING_PLAN_APPROVAL_STATUS,
    HUMAN_APPROVED_STATUS,
)
from pynchy.plugins.integrations.linear_work_item_provider import state_id


@runtime_checkable
class LinearPlanReviewClient(Protocol):
    async def query(self, query: str, **variables: object) -> dict[str, Any]: ...

    async def get_issue(self, issue_id: str) -> dict[str, Any] | None: ...

    async def create_comment(self, issue_id: str, body: str) -> dict[str, Any]: ...


LinearPlanReviewer = Callable[
    [LinearPlanReviewRequest],
    Awaitable[LinearPlanReviewResult],
]


async def review_approved_plan(  # noqa: PLR0913 - the approval boundary needs exact issue evidence.
    client: LinearPlanReviewClient,
    reviewer: LinearPlanReviewer | None,
    *,
    workspace: str,
    board: LinearWorkspaceBoard,
    issue_id: str,
    identifier: str,
    title: str,
    url: str,
    description: str,
    updated_at: str,
    public_source: bool,
    attempt: int = 1,
) -> dict[str, Any] | None:
    """Return the provider-confirmed plan revision admitted for execution."""
    if reviewer is None:
        result = LinearPlanReviewResult(
            decision=LinearPlanReviewDecision.ERROR,
            reason="Plan reviewer is unavailable",
        )
    else:
        try:
            result = await reviewer(
                LinearPlanReviewRequest(
                    workspace=workspace,
                    issue_id=issue_id,
                    identifier=identifier,
                    title=title,
                    url=url,
                    description=description,
                    updated_at=updated_at,
                    public_source=public_source,
                    attempt=attempt,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reviewer errors return to the approval boundary.
            logger.exception(
                "Linear plan freshness review failed",
                issue=identifier,
                error_type=type(exc).__name__,
            )
            result = LinearPlanReviewResult(
                decision=LinearPlanReviewDecision.ERROR,
                reason=f"{type(exc).__name__}: {exc}",
            )

    current = await client.get_issue(issue_id)
    if (
        current is None
        or current.get("updatedAt") != updated_at
        or not isinstance(current.get("state"), dict)
        or state_id(current) != state_id(board.states[HUMAN_APPROVED_STATUS])
    ):
        return None
    if result.decision is LinearPlanReviewDecision.PROCEED:
        return current

    if result.decision is LinearPlanReviewDecision.AMEND:
        # Minor drift stays approved so routine adaptations do not consume another
        # human decision. Persist first so the worker sees one canonical amended plan.
        amended = await update_issue_plan(
            client,
            issue_id=issue_id,
            state_id=state_id(board.states[HUMAN_APPROVED_STATUS]),
            description=description_with_plan(current.get("description"), result.plan or ""),
        )
        await client.create_comment(
            issue_id,
            "Plan freshness review applied a non-material amendment, "
            f"so execution will continue.\n\nReason: {result.reason}",
        )
        return amended

    awaiting_state_id = state_id(board.states[AWAITING_PLAN_APPROVAL_STATUS])
    if result.decision is LinearPlanReviewDecision.REPLAN:
        await client.create_comment(
            issue_id,
            "Plan freshness review found that the approved plan is materially stale, "
            f"so execution was not leased.\n\nReason: {result.reason}\n\n"
            "The replacement plan is awaiting review.",
        )
        await update_issue_plan(
            client,
            issue_id=issue_id,
            state_id=awaiting_state_id,
            description=description_with_plan(description, result.plan or ""),
        )
        return None

    logger.error(
        "Linear plan freshness review rejected admission",
        issue=identifier,
        error=result.reason,
    )
    raise LinearPlanReviewError(f"Plan freshness review failed for {identifier}: {result.reason}")
