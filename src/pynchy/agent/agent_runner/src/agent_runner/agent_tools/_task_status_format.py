"""Bounded structured scheduled-work status for agent clients."""

from __future__ import annotations

import json
from typing import Any

TASK_STATUS_SCHEMA_VERSION = "pynchy.scheduled_work_status.v1"
TASK_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "$defs": {
        "attention": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "paused",
                    "missing_next_run",
                    "recent_failure",
                    "scheduler_error",
                    "failure_shaped_result",
                ],
            },
        },
        "task": {
            "type": "object",
            "required": [
                "id",
                "group",
                "schedule_type",
                "schedule_value",
                "status",
                "next_run",
                "orchestration_state",
            ],
            "properties": {
                "id": {"type": "string"},
                "group": {"type": "string"},
                "schedule_type": {"type": "string"},
                "schedule_value": {"type": "string"},
                "status": {"type": "string"},
                "next_run": {"type": ["string", "null"]},
                "last_run": {"type": "string"},
                "last_result": {"type": "string"},
                "last_run_status": {"type": "string"},
                "consecutive_failures": {"type": "integer", "minimum": 1},
                "last_error_signature": {"type": "string"},
                "escalation_reason": {"type": "string"},
                "orchestration_state": {"type": ["string", "null"]},
                "orchestration_error": {"type": "string"},
                "attention": {"$ref": "#/$defs/attention"},
            },
            "additionalProperties": False,
        },
        "host_job": {
            "type": "object",
            "required": [
                "id",
                "name",
                "schedule_type",
                "schedule_value",
                "status",
                "enabled",
                "next_run",
                "orchestration_state",
            ],
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "schedule_type": {"type": "string"},
                "schedule_value": {"type": "string"},
                "status": {"type": "string"},
                "enabled": {"type": "boolean"},
                "next_run": {"type": ["string", "null"]},
                "last_run": {"type": "string"},
                "orchestration_state": {"type": ["string", "null"]},
                "orchestration_error": {"type": "string"},
                "attention": {"$ref": "#/$defs/attention"},
            },
            "additionalProperties": False,
        },
    },
    "required": ["schema", "complete", "counts", "tasks", "host_jobs", "coverage"],
    "properties": {
        "schema": {"type": "string", "const": TASK_STATUS_SCHEMA_VERSION},
        "complete": {"type": "boolean", "const": True},
        "counts": {
            "type": "object",
            "required": ["tasks", "host_jobs"],
            "properties": {
                "tasks": {"type": "integer", "minimum": 0},
                "host_jobs": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "tasks": {"type": "array", "items": {"$ref": "#/$defs/task"}},
        "host_jobs": {"type": "array", "items": {"$ref": "#/$defs/host_job"}},
        "coverage": {"type": "object"},
    },
    "additionalProperties": False,
}

_FAILURE_SHAPED_TERMS = (
    "blocked",
    "could not",
    "error",
    "fail",
    "timed out",
    "timeout",
    "unauthorized",
    "unavailable",
)


def _bounded_evidence(value: object, *, limit: int = 120) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _failure_shaped(value: object) -> bool:
    text = str(value or "").casefold()
    return any(term in text for term in _FAILURE_SHAPED_TERMS)


def _attention_reasons(result: dict[str, Any], *, last_result: object = None) -> list[str]:
    reasons = []
    if result["status"] == "paused":
        reasons.append("paused")
    if result["status"] == "active" and not result["next_run"]:
        reasons.append("missing_next_run")
    if result.get("last_run_status") == "error" or (
        isinstance(failures := result.get("consecutive_failures"), int) and failures > 0
    ):
        reasons.append("recent_failure")
    if result.get("orchestration_error"):
        reasons.append("scheduler_error")
    if _failure_shaped(last_result):
        reasons.append("failure_shaped_result")
    return reasons


def _compact_task(task: dict[str, Any]) -> dict[str, Any]:
    orchestration = _mapping(task.get("orchestration"))
    health = _mapping(task.get("run_health"))
    result = {
        "id": task.get("id"),
        "group": task.get("group"),
        "schedule_type": task.get("schedule_type"),
        "schedule_value": task.get("schedule_value"),
        "status": task.get("status"),
        "next_run": task.get("next_run"),
        "orchestration_state": orchestration.get("state"),
    }
    if last_run := task.get("last_run"):
        result["last_run"] = last_run
    if last_result := _bounded_evidence(task.get("last_result")):
        result["last_result"] = last_result
    if (last_status := health.get("last_status")) not in (None, "success"):
        result["last_run_status"] = last_status
    if failures := health.get("consecutive_failures"):
        result["consecutive_failures"] = failures
    if signature := _bounded_evidence(health.get("last_error_signature")):
        result["last_error_signature"] = signature
    if escalation := _bounded_evidence(health.get("escalation_reason")):
        result["escalation_reason"] = escalation
    if orchestration_error := _bounded_evidence(orchestration.get("error")):
        result["orchestration_error"] = orchestration_error
    result["attention"] = _attention_reasons(result, last_result=task.get("last_result"))
    if not result["attention"]:
        del result["attention"]
    return result


def _compact_host_job(job: dict[str, Any]) -> dict[str, Any]:
    orchestration = _mapping(job.get("orchestration"))
    result = {
        "id": job.get("id"),
        "name": job.get("name"),
        "schedule_type": job.get("schedule_type"),
        "schedule_value": job.get("schedule_value"),
        "status": job.get("status"),
        "enabled": job.get("enabled"),
        "next_run": job.get("next_run"),
        "orchestration_state": orchestration.get("state"),
    }
    if last_run := job.get("last_run"):
        result["last_run"] = last_run
    if orchestration_error := _bounded_evidence(orchestration.get("error")):
        result["orchestration_error"] = orchestration_error
    result["attention"] = _attention_reasons(result)
    if not result["attention"]:
        del result["attention"]
    return result


def compact_live_task_status(text: str) -> dict[str, Any] | None:
    """Parse the host projection into a complete, bounded JSON payload."""
    try:
        status = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(status, dict):
        return None
    tasks = status.get("tasks")
    host_jobs = status.get("host_jobs")
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        return None
    if not isinstance(host_jobs, list) or not all(isinstance(job, dict) for job in host_jobs):
        return None

    coverage = _mapping(status.get("coverage"))
    coverage = {
        **coverage,
        "last_result_max_chars": 120,
        "all_task_and_host_job_rows_included": True,
    }
    return {
        "schema": TASK_STATUS_SCHEMA_VERSION,
        "complete": True,
        "counts": {"tasks": len(tasks), "host_jobs": len(host_jobs)},
        "tasks": [_compact_task(task) for task in tasks],
        "host_jobs": [_compact_host_job(job) for job in host_jobs],
        "coverage": coverage,
    }
