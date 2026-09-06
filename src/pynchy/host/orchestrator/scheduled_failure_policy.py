"""Failure grouping and circuit decisions for scheduled agent work."""

from __future__ import annotations

import re

from pynchy.scheduling.api import (
    TaskRunLog,
)

STAGNATION_THRESHOLD = 3
NO_PROGRESS_THRESHOLD = 5


def error_signature(error: str) -> str:
    """Normalize volatile details so repeated failures can be grouped."""
    first_line = next((line.strip() for line in error.splitlines() if line.strip()), "")
    return re.sub(r"\s+", " ", re.sub(r"\b\d+\b", "#", first_line)).strip()


def recent_failure_run(logs: list[TaskRunLog]) -> list[TaskRunLog]:
    """Return the leading consecutive error run from newest-first logs."""
    failure_run: list[TaskRunLog] = []
    for log in logs:
        if log.status != "error":
            break
        failure_run.append(log)
    return failure_run


def _stagnation_decision(failure_run: list[TaskRunLog]) -> tuple[str, str] | None:
    if len(failure_run) < STAGNATION_THRESHOLD:
        return None

    last_signature = failure_run[0].error_signature or error_signature(failure_run[0].error or "")
    same = 0
    for log in failure_run:
        signature = log.error_signature or error_signature(log.error or "")
        if signature != last_signature:
            break
        same += 1
    if same < STAGNATION_THRESHOLD:
        return None
    return (
        "stagnation",
        f'Same error repeated {same} times in a row: "{last_signature}".',
    )


def scheduled_failure_decision(failure_run: list[TaskRunLog]) -> tuple[str, str] | None:
    """Return a circuit-breaker trigger for a consecutive failure run."""
    if stagnation := _stagnation_decision(failure_run):
        return stagnation
    if len(failure_run) < NO_PROGRESS_THRESHOLD:
        return None
    return (
        "no-progress",
        f"{len(failure_run)} consecutive scheduled-task failures with no success.",
    )
