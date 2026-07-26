"""Repeated-failure circuit breaker for scheduled agent work."""

from __future__ import annotations

import re

from pynchy.types import TaskRunLog  # noqa: TC001, RUF100 - beartype resolves annotations

_STAGNATION_THRESHOLD = 3
_NO_PROGRESS_THRESHOLD = 5


def error_signature(error: str) -> str:
    """Normalize volatile details so repeated failures can be grouped."""
    first_line = next((line.strip() for line in error.splitlines() if line.strip()), "")
    return re.sub(r"\s+", " ", re.sub(r"\b\d+\b", "#", first_line)).strip()


def _recent_failure_run(logs: list[TaskRunLog]) -> list[TaskRunLog]:
    failure_run: list[TaskRunLog] = []
    for log in logs:
        if log.status != "error":
            break
        failure_run.append(log)
    return failure_run


def _stagnation_decision(failure_run: list[TaskRunLog]) -> tuple[str, str] | None:
    if len(failure_run) < _STAGNATION_THRESHOLD:
        return None
    last_signature = failure_run[0].error_signature or error_signature(failure_run[0].error or "")
    same = 0
    for log in failure_run:
        signature = log.error_signature or error_signature(log.error or "")
        if signature != last_signature:
            break
        same += 1
    if same < _STAGNATION_THRESHOLD:
        return None
    return "stagnation", f'Same error repeated {same} times in a row: "{last_signature}".'


def _no_progress_decision(failure_run: list[TaskRunLog]) -> tuple[str, str] | None:
    if len(failure_run) < _NO_PROGRESS_THRESHOLD:
        return None
    return (
        "no-progress",
        f"{len(failure_run)} consecutive scheduled-task failures with no success.",
    )


def scheduled_task_circuit_decision(logs: list[TaskRunLog]) -> tuple[str, str] | None:
    """Return the circuit decision for a task's recent run history."""
    failure_run = _recent_failure_run(logs)
    return _stagnation_decision(failure_run) or _no_progress_decision(failure_run)
