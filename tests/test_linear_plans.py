"""Behavioral tests for durable Linear plan persistence."""

from __future__ import annotations

import pytest

from pynchy.plugins.integrations.linear_plans import (
    PLAN_END,
    PLAN_START,
    description_with_plan,
)


def test_existing_plan_is_replaced_without_losing_issue_context() -> None:
    description = (
        f"User constraints.\n\n{PLAN_START}\n## Pynchy implementation plan\n\n"
        f"Old plan.\n{PLAN_END}\n\nAcceptance notes."
    )

    updated = description_with_plan(description, "1. New plan.\n2. Verify it.")

    assert updated.startswith("User constraints.")
    assert "Old plan." not in updated
    assert "1. New plan." in updated
    assert updated.endswith("Acceptance notes.")
    assert updated.count(PLAN_START) == 1
    assert updated.count(PLAN_END) == 1


@pytest.mark.parametrize(
    ("description", "plan", "error"),
    [
        (f"Context\n\n{PLAN_START}\nIncomplete", "New plan", "section is incomplete"),
        ("Context", f"Do not inject {PLAN_END}", "cannot contain Pynchy plan markers"),
    ],
)
def test_plan_markers_cannot_create_an_ambiguous_issue_description(
    description: str,
    plan: str,
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        description_with_plan(description, plan)
