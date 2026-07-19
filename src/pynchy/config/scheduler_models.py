"""Scheduler, command-word, and queue configuration models."""

from __future__ import annotations

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

CRON_COMMAND_MESSAGE = "Cron job command cannot be empty"
TIMEOUT_POSITIVE_MESSAGE = "timeout_seconds must be positive"


class _StrictModel(BaseModel):
    """Base for scheduler sub-models -- reject unknown keys so typos fail loudly."""

    model_config = {"extra": "forbid"}


class _ResetWords(_StrictModel):
    verbs: list[str] = ["reset", "restart", "clear", "new", "wipe"]
    nouns: list[str] = ["context", "session", "chat", "conversation"]
    aliases: list[str] = ["boom", "c", "new", "clear", "reset"]


class _EndSessionWords(_StrictModel):
    verbs: list[str] = ["end", "stop", "close", "finish"]
    nouns: list[str] = ["session"]
    aliases: list[str] = ["done", "bye", "goodbye", "cya"]


class _RedeployWords(_StrictModel):
    aliases: list[str] = ["r"]
    verbs: list[str] = ["redeploy", "deploy"]


class CommandWordsConfig(_StrictModel):
    reset: _ResetWords = _ResetWords()
    end_session: _EndSessionWords = _EndSessionWords()
    redeploy: _RedeployWords = _RedeployWords()


class SchedulerConfig(_StrictModel):
    # NOTE: Update docs/usage/scheduled-tasks.md § Temporal Scheduler if you change these fields.
    poll_interval: float = 60.0  # seconds
    timezone: str = ""  # empty -> auto-detect
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "pynchy-scheduler"
    git_sync_interval_seconds: int = 300
    channel_reconciliation_interval_seconds: int = 300
    # Operators approve repository revisions unless they explicitly retain automatic deployment.
    auto_deploy: bool = False

    @field_validator("git_sync_interval_seconds", "channel_reconciliation_interval_seconds")
    @classmethod
    def validate_interval_seconds(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("scheduler intervals must be positive")
        return v


class CanaryConfig(_StrictModel):
    """Opt-in schedule and target identity for external-service canaries."""

    enabled: bool = False
    schedule: str = "0 5 * * *"
    target_profile: str = ""
    scenario_ids: list[str] = Field(default_factory=list)
    calendar_name: str = ""
    linear_team_key: str = ""
    linear_workspace: str = ""
    proton_mailbox: str = "INBOX"
    proton_recipient: str = ""
    google_calendar_server: str = ""
    google_calendar_id: str = ""
    google_drive_server: str = ""
    google_drive_probe_query: str = ""
    google_drive_file_id: str = ""

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, v: str) -> str:
        if not croniter.is_valid(v):
            msg = f"Invalid cron expression: {v}"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def require_target_profile_when_enabled(self) -> CanaryConfig:
        if self.enabled and not self.target_profile.strip():
            raise ValueError("target_profile is required when canaries are enabled")
        return self


class CronJobConfig(_StrictModel):
    schedule: str  # cron expression
    command: str
    cwd: str | None = None  # optional working directory (relative to project root or absolute)
    timeout_seconds: int = 600
    enabled: bool = True
    quiet_on_success: bool = False

    @field_validator("schedule")
    @classmethod
    def validate_schedule(cls, v: str) -> str:
        if not croniter.is_valid(v):
            msg = f"Invalid cron expression: {v}"
            raise ValueError(msg)
        return v

    @field_validator("command")
    @classmethod
    def validate_command(cls, v: str) -> str:
        command = v.strip()
        if not command:
            raise ValueError(CRON_COMMAND_MESSAGE)
        return command

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout_seconds(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(TIMEOUT_POSITIVE_MESSAGE)
        return v


class IntervalsConfig(_StrictModel):
    message_poll: float = 2.0  # seconds
    ipc_poll: float = 1.0  # seconds


class QueueConfig(_StrictModel):
    max_retries: int = 5
    base_retry_seconds: float = 5.0
