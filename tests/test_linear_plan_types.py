import pytest

from pynchy.linear_plan_types import LinearPlanReviewDecision, LinearPlanReviewResult


def test_linear_plan_review_requires_a_reason() -> None:
    with pytest.raises(ValueError, match="reason cannot be empty"):
        LinearPlanReviewResult(LinearPlanReviewDecision.PROCEED, " ")


@pytest.mark.parametrize(
    "decision",
    [LinearPlanReviewDecision.AMEND, LinearPlanReviewDecision.REPLAN],
)
def test_plan_changing_review_requires_a_replacement_plan(
    decision: LinearPlanReviewDecision,
) -> None:
    with pytest.raises(ValueError, match="requires a replacement plan"):
        LinearPlanReviewResult(decision, "Update the plan")


def test_non_plan_changing_review_rejects_a_plan() -> None:
    with pytest.raises(ValueError, match="Only a plan-changing decision"):
        LinearPlanReviewResult(
            LinearPlanReviewDecision.PROCEED,
            "Proceed as planned",
            plan="Unneeded plan",
        )
