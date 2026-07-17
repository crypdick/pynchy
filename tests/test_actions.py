"""Tests for the semantic action catalog and its coverage calculations."""

from __future__ import annotations

from pynchy.actions import (
    ACTION_SPECS,
    ActionId,
    ActionSpec,
    EvidenceRequirement,
    assess_hermetic_coverage,
    validate_action_specs,
)


def test_builtin_action_catalog_is_valid():
    assert validate_action_specs(ACTION_SPECS) == ()


def test_catalog_rejects_duplicate_invalid_and_incomplete_agentic_actions():
    specs = (
        ActionSpec(ActionId("not-an-action"), "", ""),
        ActionSpec(ActionId("task.create"), "tasks", "Create a task."),
        ActionSpec(
            ActionId("task.create"),
            "tasks",
            "Duplicate task action.",
            EvidenceRequirement.HERMETIC_AND_AGENTIC,
        ),
    )

    errors = validate_action_specs(specs)

    assert "invalid action id: 'not-an-action'" in errors
    assert "not-an-action: owner is required" in errors
    assert "not-an-action: summary is required" in errors
    assert "duplicate action id: task.create" in errors
    assert "task.create: agentic coverage requires a canary scenario" in errors


def test_hermetic_coverage_reports_missing_and_unknown_actions():
    specs = (
        ActionSpec(ActionId("task.create"), "tasks", "Create a task."),
        ActionSpec(ActionId("task.cancel"), "tasks", "Cancel a task."),
    )

    report = assess_hermetic_coverage(specs, ["task.create", "task.unknown"])

    assert report.missing == ("task.cancel",)
    assert report.unknown == ("task.unknown",)
    assert report.is_complete is False
    assert report.describe() == (
        "actions without hermetic tests: task.cancel; tests mark unknown actions: task.unknown"
    )
