"""Compatibility coverage for the historical new-feature runtime command."""

from __future__ import annotations

from scripts import new_feature_sandbox, runtime_harness


def test_new_feature_sandbox_delegates_to_shared_runtime_harness() -> None:
    """Feature worktrees and CI must enter the same deterministic lifecycle."""
    assert new_feature_sandbox.main is runtime_harness.main
