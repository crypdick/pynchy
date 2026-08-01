"""Types shared by Linear plan review admission and orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import cast


@dataclass(frozen=True)
class LinearPlanReviewAdmission:
    """Immutable provider revision queued for independent plan review."""

    workspace: str
    issue_id: str
    identifier: str
    updated_at: str
    public_source: bool

    def to_payload(self) -> dict[str, object]:
        """Return the Temporal-safe representation."""
        return {
            "workspace": self.workspace,
            "issue_id": self.issue_id,
            "identifier": self.identifier,
            "updated_at": self.updated_at,
            "public_source": self.public_source,
        }

    @classmethod
    def from_payload(cls, payload: object) -> LinearPlanReviewAdmission:
        """Parse one Temporal payload at the activity boundary."""
        if not isinstance(payload, dict):
            raise TypeError("Linear plan review admission payload must be an object")
        text = tuple(
            payload.get(key) for key in ("workspace", "issue_id", "identifier", "updated_at")
        )
        if any(not isinstance(value, str) or not value for value in text):
            raise ValueError("Linear plan review admission payload has invalid text fields")
        public_source = payload.get("public_source")
        if not isinstance(public_source, bool):
            raise TypeError("Linear plan review admission public_source must be boolean")
        workspace, issue_id, identifier, updated_at = cast(
            "tuple[str, str, str, str]",
            text,
        )
        return cls(
            workspace=workspace,
            issue_id=issue_id,
            identifier=identifier,
            updated_at=updated_at,
            public_source=public_source,
        )


class LinearPlanReviewDecision(StrEnum):
    """Admission decisions returned by an independent Linear plan review."""

    PROCEED = "proceed"
    AMEND = "amend"
    REPLAN = "replan"
    ERROR = "error"


class LinearPlanReviewBlockedError(RuntimeError):
    """The final reviewer failure was settled as a blocked issue."""


class LinearPlanReviewError(RuntimeError):
    """The reviewer could not make an admission decision."""


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
    attempt: int = 1


@dataclass(frozen=True)
class LinearPlanReviewResult:
    """Typed reviewer output consumed before an execution lease exists."""

    decision: LinearPlanReviewDecision
    reason: str
    plan: str | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("Linear plan review reason cannot be empty")
        if self.decision in {
            LinearPlanReviewDecision.AMEND,
            LinearPlanReviewDecision.REPLAN,
        }:
            if self.plan is None or not self.plan.strip():
                raise ValueError("A plan-changing decision requires a replacement plan")
        elif self.plan is not None:
            raise ValueError("Only a plan-changing decision may include a replacement plan")
