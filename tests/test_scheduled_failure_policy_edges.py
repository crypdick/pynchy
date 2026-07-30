"""Failure-grouping boundaries for scheduled work."""

from __future__ import annotations

from pynchy.host.orchestrator.scheduled_failure_policy import scheduled_failure_decision
from pynchy.scheduling.api import TaskRunLog


def test_mixed_error_signatures_do_not_trigger_stagnation() -> None:
    logs = [
        TaskRunLog("task-1", "now", 1, "error", error_signature="RuntimeError: first"),
        TaskRunLog("task-1", "earlier", 1, "error", error_signature="RuntimeError: second"),
        TaskRunLog("task-1", "oldest", 1, "error", error_signature="RuntimeError: first"),
    ]

    assert scheduled_failure_decision(logs) is None


def test_five_failures_trigger_no_progress_breaker() -> None:
    logs = [
        TaskRunLog(f"task-{index}", f"time-{index}", 1, "error", error_signature=f"error-{index}")
        for index in range(5)
    ]

    assert scheduled_failure_decision(logs) == (
        "no-progress",
        "5 consecutive scheduled-task failures with no success.",
    )
