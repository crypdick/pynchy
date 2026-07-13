"""Compatibility coverage for the historical new-feature runtime command."""

from __future__ import annotations

import tomllib
from pathlib import Path

from scripts import new_feature_sandbox, runtime_harness


def test_new_feature_sandbox_delegates_to_shared_runtime_harness() -> None:
    """Feature worktrees and CI must enter the same deterministic lifecycle."""
    assert new_feature_sandbox.main is runtime_harness.main


def test_pre_merge_uses_a_fresh_runtime_that_cleans_up_its_live_resources() -> None:
    """Merge verification must test current source rather than the setup-time image."""
    project = tomllib.loads(Path(__file__).parents[1].joinpath("pyproject.toml").read_text())
    commands = project["tool"]["new-feature"]["pre_merge"]

    assert commands[:2] == [
        "uv run python scripts/new_feature_sandbox.py stop",
        (
            "uv run python scripts/new_feature_sandbox.py run -- "
            "uv run pytest -o addopts='' -n 0 -m runtime"
        ),
    ]
