"""Semantic values for scheduled agent and host work."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class SessionPolicy(StrEnum):
    """How a scheduled occurrence treats its thread-owned durable session."""

    CONTINUE = "continue"
    RESET_BEFORE_RUN = "reset_before_run"


@dataclass
class ScheduledTask:
    id: str
    group_folder: str
    chat_jid: str
    prompt: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    session_policy: SessionPolicy
    next_run: str | None = None
    last_run: str | None = None
    last_result: str | None = None
    status: Literal["active", "paused", "completed", "cancelled"] = "active"
    created_at: str = ""
    memory_enabled: bool = True
    repo_access: str | None = None
    input_source: str = "scheduled_task"
    config_job_name: str | None = None
    config_job_is_deterministic: bool | None = False
    config_job_command: str | None = None
    config_job_cwd: str | None = None
    config_job_timeout_seconds: int | None = None
    config_job_display_name: str | None = None
    config_job_pre_run_command: str | None = None
    config_job_pre_run_cwd: str | None = None
    config_job_pre_run_timeout_seconds: int | None = None
    derived_thread_name: str | None = None
    bound_chat_jid: str | None = None
    bound_group_folder: str | None = None
    conversation_id: str | None = None
    last_reset_occurrence: str | None = None
    occurrence_generation: int = 0
    occurrence_due_at: str | None = None
    superseded_occurrence_generation: int | None = None
    superseded_occurrence_due_at: str | None = None

    def to_snapshot_dict(self) -> dict[str, str | None]:
        """Serialize to the dict format consumed by task snapshots."""
        return {
            "id": self.id,
            "type": "agent",
            "groupFolder": self.group_folder,
            "prompt": self.prompt,
            "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value,
            "status": self.status,
            "next_run": None,
        }


_SAFE_WORKFLOW_FRAGMENT = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_workflow_fragment(value: str) -> str:
    """Return a value safe for durable workflow and schedule IDs."""
    return _SAFE_WORKFLOW_FRAGMENT.sub("-", value).strip("-").replace("+", "-")


def agent_task_occurrence_due_at(task: ScheduledTask) -> str:
    """Return the effective due time for the task's current occurrence."""
    return task.occurrence_due_at or task.schedule_value


def agent_task_occurrence_workflow_id(task_id: str, due_at: str, generation: int) -> str:
    """Return the exact durable identity for one scheduled-task occurrence."""
    base = f"pynchy-agent-task-{safe_workflow_fragment(task_id)}-{safe_workflow_fragment(due_at)}"
    if generation == 0:
        return base
    task_digest = hashlib.sha256(task_id.encode()).hexdigest()[:16]
    return (
        f"pynchy-agent-task-{safe_workflow_fragment(task_id)}-{task_digest}-"
        f"{safe_workflow_fragment(due_at)}-resume-{generation}"
    )


def agent_task_workflow_id(task: ScheduledTask) -> str:
    """Return the idempotency key for a one-time scheduled agent task."""
    return agent_task_occurrence_workflow_id(
        task.id,
        agent_task_occurrence_due_at(task),
        task.occurrence_generation,
    )


@dataclass
class TaskRunLog:
    """Attempt evidence linked across Temporal retries and Pynchy recovery."""

    task_id: str
    run_at: str
    duration_ms: int | float
    status: Literal["success", "incomplete", "error", "resumed"]
    result: str | None = None
    error: str | None = None
    temporal_workflow_id: str | None = None
    temporal_workflow_run_id: str | None = None
    temporal_attempt: int | None = None
    turn_id: str | None = None
    error_signature: str | None = None
    escalation_reason: str | None = None


@dataclass
class HostJob:
    """Host-level scheduled command that runs without an agent container."""

    id: str
    name: str
    command: str
    schedule_type: Literal["cron", "interval", "once"]
    schedule_value: str
    created_by: str  # noqa: V107
    next_run: str | None = None
    last_run: str | None = None
    status: Literal["active", "paused", "completed"] = "active"
    created_at: str = ""
    cwd: str | None = None
    timeout_seconds: int = 600
    enabled: bool = True
    memory_enabled: bool = True

    def to_snapshot_dict(self) -> dict[str, str | None]:
        """Serialize to the dict format consumed by task snapshots."""
        return {
            "id": self.id,
            "type": "host",
            "name": self.name,
            "command": self.command,
            "schedule_type": self.schedule_type,
            "schedule_value": self.schedule_value,
            "status": self.status,
            "next_run": None,
        }
