"""Compact scheduled-work status formatting for agent context."""

from __future__ import annotations

import json
from typing import Any


def _single_line(value: object, *, limit: int = 180) -> str:
    """Keep task result evidence useful without flooding the model context."""
    if value is None:
        return "-"
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text or "-"
    return f"{text[: limit - 3]}..."


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _task_status_line(task: dict[str, Any]) -> str:
    orchestration = _mapping(task.get("orchestration"))
    health = _mapping(task.get("run_health"))
    line = (
        f"- {task.get('id', '?')} | group={task.get('group', '?')} | "
        f"schedule={task.get('schedule_type', '?')}:{task.get('schedule_value', '?')} | "
        f"status={task.get('status', '?')} | next={task.get('next_run') or '-'}"
    )
    details = (
        [f"result={result}"] if (result := _single_line(task.get("last_result"))) != "-" else []
    )
    if (run_status := health.get("last_status")) not in (None, "success"):
        details.append(f"last_run_status={run_status}")
    if failures := health.get("consecutive_failures"):
        details.append(f"consecutive_failures={failures}")
    if signature := health.get("last_error_signature"):
        details.append(f"error={_single_line(signature)}")
    if escalation := health.get("escalation_reason"):
        details.append(f"escalation={_single_line(escalation)}")
    if (state := orchestration.get("state")) not in (None, "scheduled", "delayed"):
        details.append(f"orchestration={state}")
    if error := orchestration.get("error"):
        details.append(f"orchestration_error={_single_line(error)}")
    return f"{line} | {' | '.join(details)}" if details else line


def _host_job_status_line(job: dict[str, Any]) -> str:
    orchestration = _mapping(job.get("orchestration"))
    line = (
        f"- {job.get('id', '?')} | name={job.get('name', '?')} | "
        f"schedule={job.get('schedule_type', '?')}:{job.get('schedule_value', '?')} | "
        f"status={job.get('status', '?')} | enabled={job.get('enabled', '?')} | "
        f"next={job.get('next_run') or '-'}"
    )
    details = []
    if (state := orchestration.get("state")) not in (None, "scheduled", "delayed"):
        details.append(f"orchestration={state}")
    if error := orchestration.get("error"):
        details.append(f"orchestration_error={_single_line(error)}")
    return f"{line} | {' | '.join(details)}" if details else line


def compact_live_task_status(text: str) -> str:
    """Render the structured host projection as a bounded complete inventory."""
    try:
        status = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(status, dict):
        return text
    tasks = status.get("tasks")
    host_jobs = status.get("host_jobs")
    if not isinstance(tasks, list) or not isinstance(host_jobs, list):
        return text

    task_rows = [_task_status_line(task) for task in tasks if isinstance(task, dict)]
    host_rows = [_host_job_status_line(job) for job in host_jobs if isinstance(job, dict)]
    lines = [
        "Live scheduled-work snapshot (read-only; answer from this result without re-querying):",
        f"Agent tasks ({len(task_rows)}):",
        *(task_rows or ["- none"]),
        f"Database host jobs ({len(host_rows)}):",
        *(host_rows or ["- none"]),
    ]
    if coverage := status.get("coverage"):
        lines.append(f"Coverage: {_single_line(coverage, limit=400)}")
    return "\n".join(lines)
