"""Resource-aware pytest parallelism."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from conftest import cgroup_memory_limit_bytes, pytest_xdist_auto_num_workers

if TYPE_CHECKING:
    from pathlib import Path


def test_reads_finite_cgroup_memory_limit(tmp_path: Path) -> None:
    unlimited = tmp_path / "memory.max"
    limited = tmp_path / "memory.limit_in_bytes"
    unlimited.write_text("max\n", encoding="utf-8")
    limited.write_text(str(2 * 1024 * 1024 * 1024), encoding="utf-8")

    assert cgroup_memory_limit_bytes((unlimited, limited)) == 2 * 1024 * 1024 * 1024


def test_two_gib_cgroup_limits_xdist_auto_to_one_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    monkeypatch.setattr("conftest.cgroup_memory_limit_bytes", lambda: 2 * 1024 * 1024 * 1024)
    monkeypatch.setattr("conftest.os.process_cpu_count", lambda: 8)

    assert pytest_xdist_auto_num_workers(pytest.Config.fromdictargs({}, [])) == 1
