"""Configuration coverage for the deterministic runtime harness."""

from __future__ import annotations

import tomllib
from pathlib import Path


def test_pre_merge_uses_a_fresh_runtime_that_cleans_up_its_live_resources() -> None:
    """Merge verification must test current source rather than the setup-time image."""
    project = tomllib.loads(Path(__file__).parents[1].joinpath("pyproject.toml").read_text())
    commands = project["tool"]["new-feature"]["pre_merge"]

    assert commands[:2] == [
        "uv run python scripts/runtime_harness.py stop",
        (
            "uv run python scripts/runtime_harness.py run -- "
            "uv run pytest -o addopts='' -n 0 -m runtime"
        ),
    ]
