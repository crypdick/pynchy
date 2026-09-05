"""Configuration coverage for the deterministic runtime harness."""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.prek_hooks import check_coverage_ratchet
from scripts.prek_hooks.check_coverage_ratchet import (
    check_ratchet,
    measured_ratchet,
    minimum_allowed_ratchet,
    raise_ratchet,
    read_ratchet,
)

if TYPE_CHECKING:
    import pytest


def _project() -> dict:
    return tomllib.loads(Path(__file__).parents[1].joinpath("pyproject.toml").read_text())


def test_pre_merge_uses_a_fresh_runtime_that_cleans_up_its_live_resources() -> None:
    """Merge verification must test current source rather than the setup-time image."""
    commands = _project()["tool"]["new-feature"]["pre_merge"]

    assert commands[:2] == [
        "uv run python scripts/runtime_harness.py stop",
        (
            "uv run python scripts/runtime_harness.py run -- "
            "uv run pytest -o addopts='' -n 0 -m runtime"
        ),
    ]


def test_coverage_ratchet_only_moves_up(tmp_path: Path) -> None:
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text("[tool.coverage.report]\nfail_under = 81\n", encoding="utf-8")

    assert minimum_allowed_ratchet([Decimal(80), Decimal(82)]) == 82
    assert raise_ratchet(project_file, Decimal(80)) is False
    assert raise_ratchet(project_file, Decimal(83)) is True
    assert read_ratchet(project_file.read_text(encoding="utf-8")) == 83


def test_coverage_ratchet_rejects_a_lower_committed_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text("[tool.coverage.report]\nfail_under = 81\n", encoding="utf-8")
    monkeypatch.setattr(
        "scripts.prek_hooks.check_coverage_ratchet.committed_ratchets",
        lambda _project_file: [Decimal(82)],
    )

    assert check_ratchet(project_file, update=False) == 1


def test_coverage_ratchet_floors_a_measurement_at_its_display_precision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Config:
        precision = 0  # noqa: V107

    class _Coverage:
        config = _Config()

        def load(self) -> None:
            pass

        def report(self, *, file: object) -> float:
            del file
            return 82.54

    monkeypatch.setattr(check_coverage_ratchet, "Coverage", _Coverage)

    assert measured_ratchet() == Decimal(82)


def test_merge_stages_the_ratchet_after_integrated_coverage() -> None:
    commands = _project()["tool"]["new-feature"]["post_merge"]

    assert commands[:3] == [
        (
            "unset SERVER__PORT GATEWAY__PORT NEW_FEATURE_TEMPORAL_PORT "
            "PYNCHY_RUNTIME_NAMESPACE; uv run pytest --cov=pynchy "
            "--cov-report=term-missing"
        ),
        "uv run python scripts/prek_hooks/check_coverage_ratchet.py --update",
        "git add pyproject.toml",
    ]


def test_prek_checks_and_updates_the_coverage_ratchet() -> None:
    config = tomllib.loads(Path("prek.toml").read_text(encoding="utf-8"))
    hooks = config["repos"][-1]["hooks"]
    by_id = {hook["id"]: hook for hook in hooks}

    assert by_id["check-coverage-ratchet"]["always_run"] is True
    assert hooks.index(by_id["pytest"]) < hooks.index(by_id["update-coverage-ratchet"])
    assert by_id["update-coverage-ratchet"]["stages"] == ["manual"]
