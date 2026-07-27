"""Execution admission outcomes for approved Linear plans."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from pynchy.linear_plan_types import (
    LinearPlanReviewDecision,
    LinearPlanReviewRequest,
    LinearPlanReviewResult,
)
from pynchy.logger import logger
from pynchy.plugins.integrations.linear_boards import (  # noqa: TC001, RUF100 - beartype resolves this annotation at runtime.
    LinearWorkspaceBoard,
)
from pynchy.plugins.integrations.linear_issue_mutations import update_issue_state
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


async def review_approved_plan(  # noqa: PLR0913, RUF100 - the approval boundary needs exact issue evidence.
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
) -> bool:
    """Return whether unchanged approved work may proceed to lease acquisition."""
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
                )
            )
        except Exception as exc:  # noqa: BLE001, RUF100 - reviewer errors return to the approval boundary.
            logger.exception(
                "Linear plan freshness review failed",
                issue=identifier,
                error_type=type(exc).__name__,
            )
            result = LinearPlanReviewResult(
                decision=LinearPlanReviewDecision.ERROR,
                reason=f"{type(exc).__name__}: plan reviewer failed",
            )

    current = await client.get_issue(issue_id)
    if (
        current is None
        or current.get("updatedAt") != updated_at
        or not isinstance(current.get("state"), dict)
        or state_id(current) != state_id(board.states[HUMAN_APPROVED_STATUS])
    ):
        return False
    if result.decision is LinearPlanReviewDecision.PROCEED:
        return True

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
        return False

    await client.create_comment(
        issue_id,
        "Plan freshness review failed, so execution was not leased.\n\n"
        f"Error: {result.reason}\n\nMoved back to Awaiting Plan Approval.",
    )
    await update_issue_state(client, issue_id, awaiting_state_id)
    return False
