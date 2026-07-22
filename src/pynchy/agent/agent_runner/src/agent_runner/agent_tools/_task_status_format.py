"""Bounded structured scheduled-work status for agent clients."""

from __future__ import annotations

import json
import re
from typing import Any

TASK_STATUS_SCHEMA_VERSION = "pynchy.scheduled_work_status.v1"
MAX_TASK_ROWS = 64
MAX_HOST_JOB_ROWS = 32
MAX_EVIDENCE_CHARS = 120

_IDENTIFIER_CHARS = 128
_GROUP_CHARS = 64
_SCHEDULE_CHARS = 128
_STATUS_CHARS = 32
_TIMESTAMP_CHARS = 64

_ATTENTION_VALUES = [
    "paused",
    "missing_next_run",
    "recent_failure",
    "scheduler_error",
    "failure_shaped_result",
]
TASK_STATUS_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "$defs": {
        "attention": {
            "type": "array",
            "items": {"type": "string", "enum": _ATTENTION_VALUES},
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
                "id": {"type": "string", "maxLength": _IDENTIFIER_CHARS},
                "group": {"type": "string", "maxLength": _GROUP_CHARS},
                "schedule_type": {"type": "string", "maxLength": _STATUS_CHARS},
                "schedule_value": {"type": "string", "maxLength": _SCHEDULE_CHARS},
                "status": {"type": "string", "maxLength": _STATUS_CHARS},
                "next_run": {"type": ["string", "null"], "maxLength": _TIMESTAMP_CHARS},
                "last_run": {"type": "string", "maxLength": _TIMESTAMP_CHARS},
                "last_result": {"type": "string", "maxLength": MAX_EVIDENCE_CHARS},
                "last_run_status": {"type": "string", "maxLength": _STATUS_CHARS},
                "consecutive_failures": {"type": "integer", "minimum": 1, "maximum": 9999},
                "last_error_signature": {"type": "string", "maxLength": MAX_EVIDENCE_CHARS},
                "escalation_reason": {"type": "string", "maxLength": MAX_EVIDENCE_CHARS},
                "orchestration_state": {"type": "string", "maxLength": _STATUS_CHARS},
                "orchestration_error": {"type": "string", "maxLength": MAX_EVIDENCE_CHARS},
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
                "id": {"type": "string", "maxLength": _IDENTIFIER_CHARS},
                "name": {"type": "string", "maxLength": _IDENTIFIER_CHARS},
                "schedule_type": {"type": "string", "maxLength": _STATUS_CHARS},
                "schedule_value": {"type": "string", "maxLength": _SCHEDULE_CHARS},
                "status": {"type": "string", "maxLength": _STATUS_CHARS},
                "enabled": {"type": "boolean"},
                "next_run": {"type": ["string", "null"], "maxLength": _TIMESTAMP_CHARS},
                "last_run": {"type": "string", "maxLength": _TIMESTAMP_CHARS},
                "orchestration_state": {"type": "string", "maxLength": _STATUS_CHARS},
                "orchestration_error": {"type": "string", "maxLength": MAX_EVIDENCE_CHARS},
                "attention": {"$ref": "#/$defs/attention"},
            },
            "additionalProperties": False,
        },
    },
    "required": ["schema", "completeness", "counts", "tasks", "host_jobs", "coverage"],
    "properties": {
        "schema": {"type": "string", "const": TASK_STATUS_SCHEMA_VERSION},
        "completeness": {
            "type": "object",
            "required": ["complete_for_scope", "scope", "omitted_populations"],
            "properties": {
                "complete_for_scope": {"type": "boolean", "const": True},
                "scope": {"type": "string"},
                "omitted_populations": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
        "counts": {
            "type": "object",
            "required": ["tasks", "host_jobs"],
            "properties": {
                "tasks": {"type": "integer", "minimum": 0, "maximum": MAX_TASK_ROWS},
                "host_jobs": {"type": "integer", "minimum": 0, "maximum": MAX_HOST_JOB_ROWS},
            },
            "additionalProperties": False,
        },
        "tasks": {
            "type": "array",
            "maxItems": MAX_TASK_ROWS,
            "items": {"$ref": "#/$defs/task"},
        },
        "host_jobs": {
            "type": "array",
            "maxItems": MAX_HOST_JOB_ROWS,
            "items": {"$ref": "#/$defs/host_job"},
        },
        "coverage": {"type": "object"},
    },
    "additionalProperties": False,
}

_NEGATED_FAILURE = re.compile(
    r"\b(?:(?:no|not|zero|0)\s+(?:errors?|failures?|blockers?|blocked)"
    r"|without\s+(?:errors?|failures?|blockers?))\b",
    re.IGNORECASE,
)
_FAILURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bblocked\b",
        r"\berrors?\b",
        r"\bfail(?:ed|ure|ures|ing)?\b",
        r"\b(?:unable|unavailable)\b",
        r"\bcould not\b",
        r"\bmissing\s+(?:a\s+)?(?:credentials?|token|api[ -]?key|secret)\b",
        r"\bpermission denied\b",
        r"\blogin required\b",
        r"\bconnection refused\b",
        r"\brate[ -]?limit(?:ed|ing)?\b",
        r"\bunauthorized\b",
        r"\btim(?:e|ed) out\b",
        r"\btimeout\b",
    )
)


class TaskStatusFormatError(ValueError):
    """The host projection cannot fit the declared structured contract."""


def _required_string(record: dict[str, Any], field: str, *, limit: int) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise TaskStatusFormatError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise TaskStatusFormatError(f"{field} exceeds the {limit}-character contract")
    return value


def _optional_string(value: object, field: str, *, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskStatusFormatError(f"{field} must be a string or null")
    if len(value) > limit:
        raise TaskStatusFormatError(f"{field} exceeds the {limit}-character contract")
    return value or None


def _bounded_evidence(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TaskStatusFormatError("status evidence must be a string or null")
    text = " ".join(value.split())
    if not text:
        return None
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text
    return f"{text[: MAX_EVIDENCE_CHARS - 3]}..."


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskStatusFormatError(f"{field} must be an object")
    return value


def _failure_shaped(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = _NEGATED_FAILURE.sub("", value)
    return any(pattern.search(text) for pattern in _FAILURE_PATTERNS)


def _attention_reasons(result: dict[str, Any], *, last_result: object = None) -> list[str]:
    reasons = []
    if result["status"] == "paused":
        reasons.append("paused")
    if result["status"] == "active" and not result["next_run"]:
        reasons.append("missing_next_run")
    if result.get("last_run_status") == "error" or result.get("consecutive_failures", 0) > 0:
        reasons.append("recent_failure")
    if result.get("orchestration_error"):
        reasons.append("scheduler_error")
    if _failure_shaped(last_result):
        reasons.append("failure_shaped_result")
    return reasons


def _compact_task(task: dict[str, Any]) -> dict[str, Any]:
    orchestration = _mapping(task.get("orchestration"), "orchestration")
    health = _mapping(task.get("run_health"), "run_health")
    failures = health.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or not 0 <= failures <= 9999:
        raise TaskStatusFormatError("consecutive_failures must be an integer from 0 to 9999")
    result: dict[str, Any] = {
        "id": _required_string(task, "id", limit=_IDENTIFIER_CHARS),
        "group": _required_string(task, "group", limit=_GROUP_CHARS),
        "schedule_type": _required_string(task, "schedule_type", limit=_STATUS_CHARS),
        "schedule_value": _required_string(task, "schedule_value", limit=_SCHEDULE_CHARS),
        "status": _required_string(task, "status", limit=_STATUS_CHARS),
        "next_run": _optional_string(task.get("next_run"), "next_run", limit=_TIMESTAMP_CHARS),
        "orchestration_state": _required_string(orchestration, "state", limit=_STATUS_CHARS),
    }
    optional_fields = {
        "last_run": _optional_string(task.get("last_run"), "last_run", limit=_TIMESTAMP_CHARS),
        "last_result": _bounded_evidence(task.get("last_result")),
        "last_run_status": _optional_string(
            health.get("last_status"), "last_status", limit=_STATUS_CHARS
        ),
        "last_error_signature": _bounded_evidence(health.get("last_error_signature")),
        "escalation_reason": _bounded_evidence(health.get("escalation_reason")),
        "orchestration_error": _bounded_evidence(orchestration.get("error")),
    }
    result.update({key: value for key, value in optional_fields.items() if value is not None})
    if result.get("last_run_status") == "success":
        del result["last_run_status"]
    if failures:
        result["consecutive_failures"] = failures
    if attention := _attention_reasons(result, last_result=task.get("last_result")):
        result["attention"] = attention
    return result


def _compact_host_job(job: dict[str, Any]) -> dict[str, Any]:
    orchestration = _mapping(job.get("orchestration"), "orchestration")
    enabled = job.get("enabled")
    if not isinstance(enabled, bool):
        raise TaskStatusFormatError("enabled must be a boolean")
    result: dict[str, Any] = {
        "id": _required_string(job, "id", limit=_IDENTIFIER_CHARS),
        "name": _required_string(job, "name", limit=_IDENTIFIER_CHARS),
        "schedule_type": _required_string(job, "schedule_type", limit=_STATUS_CHARS),
        "schedule_value": _required_string(job, "schedule_value", limit=_SCHEDULE_CHARS),
        "status": _required_string(job, "status", limit=_STATUS_CHARS),
        "enabled": enabled,
        "next_run": _optional_string(job.get("next_run"), "next_run", limit=_TIMESTAMP_CHARS),
        "orchestration_state": _required_string(orchestration, "state", limit=_STATUS_CHARS),
    }
    optional_fields = {
        "last_run": _optional_string(job.get("last_run"), "last_run", limit=_TIMESTAMP_CHARS),
        "orchestration_error": _bounded_evidence(orchestration.get("error")),
    }
    result.update({key: value for key, value in optional_fields.items() if value is not None})
    if attention := _attention_reasons(result):
        result["attention"] = attention
    return result


def compact_live_task_status(text: str) -> dict[str, Any]:
    """Parse the host projection into the declared bounded JSON payload."""
    try:
        status = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskStatusFormatError("host task status is not valid JSON") from exc
    if not isinstance(status, dict):
        raise TaskStatusFormatError("host task status must be an object")
    tasks = status.get("tasks")
    host_jobs = status.get("host_jobs")
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        raise TaskStatusFormatError("tasks must be an array of objects")
    if not isinstance(host_jobs, list) or not all(isinstance(job, dict) for job in host_jobs):
        raise TaskStatusFormatError("host_jobs must be an array of objects")
    if len(tasks) > MAX_TASK_ROWS:
        raise TaskStatusFormatError(f"task count exceeds the {MAX_TASK_ROWS}-row contract")
    if len(host_jobs) > MAX_HOST_JOB_ROWS:
        raise TaskStatusFormatError(f"host job count exceeds the {MAX_HOST_JOB_ROWS}-row contract")

    return {
        "schema": TASK_STATUS_SCHEMA_VERSION,
        "completeness": {
            "complete_for_scope": True,
            "scope": (
                "rows visible to this caller from scheduled_tasks and database-backed host_jobs"
            ),
            "omitted_populations": [
                "static config or plugin host schedules",
                "Temporal schedules without a visible database-backed definition",
            ],
        },
        "counts": {"tasks": len(tasks), "host_jobs": len(host_jobs)},
        "tasks": [_compact_task(task) for task in tasks],
        "host_jobs": [_compact_host_job(job) for job in host_jobs],
        "coverage": {
            "task_attempts": "latest result and five-attempt health summary",
            "host_job_attempts": "Temporal orchestration state only",
            "task_prompts_included": False,
            "host_commands_included": False,
            "last_result_max_chars": MAX_EVIDENCE_CHARS,
            "all_rows_in_scope_included": True,
            "max_task_rows": MAX_TASK_ROWS,
            "max_host_job_rows": MAX_HOST_JOB_ROWS,
        },
    }
