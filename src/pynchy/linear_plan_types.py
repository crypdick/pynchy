"""Types shared by Linear plan review admission and orchestration."""

from dataclasses import dataclass
from enum import StrEnum


class LinearPlanReviewDecision(StrEnum):
    """Admission decisions returned by an independent Linear plan review."""

    PROCEED = "proceed"
    REPLAN = "replan"
    ERROR = "error"


@dataclass(frozen=True)
class LinearPlanReviewRequest:
    """Current provider evidence supplied to a hidden plan reviewer."""

    workspace: str
    issue_id: str
    identifier: str
    title: str
    url: str
    description: str
    updated_at: str
    public_source: bool


@dataclass(frozen=True)
class LinearPlanReviewResult:
    """Typed reviewer output consumed before an execution lease exists."""

    decision: LinearPlanReviewDecision
    reason: str
    plan: str | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("Linear plan review reason cannot be empty")
        if self.decision is LinearPlanReviewDecision.REPLAN:
            if self.plan is None or not self.plan.strip():
                raise ValueError("A replan decision requires a replacement plan")
        elif self.plan is not None:
            raise ValueError("Only a replan decision may include a replacement plan")
