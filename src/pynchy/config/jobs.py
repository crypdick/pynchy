"""Config-backed job models."""

from __future__ import annotations

from datetime import datetime

from croniter import croniter
from pydantic import field_validator, model_validator

from pynchy.config.models import _StrictModel

_AGENT_JOB_WORKSPACE_REQUIRED_ERROR = "agent jobs require workspace"
_JOB_COMMAND_EMPTY_ERROR = "host job command cannot be empty"
_JOB_TIMEOUT_SECONDS_ERROR = "timeout_seconds must be positive"
_JOB_AT_ERROR = "job at must be an ISO datetime"
_JOB_SHAPE_ERROR = "jobs require exactly one of schedule, interval_minutes, or at"
_HOST_JOB_COMMAND_ERROR = "host jobs require command"
_HOST_JOB_SCHEDULE_ERROR = "host jobs require schedule"
_HOST_JOB_PROMPT_ERROR = "host jobs cannot set prompt or prompt_file"
_AGENT_JOB_COMMAND_ERROR = "agent jobs cannot set command"
_AGENT_JOB_PROMPT_ERROR = "agent jobs require prompt or prompt_file"
_HOST_JOB_PRE_RUN_ERROR = "host jobs cannot set pre-run fields"


class JobConfig(_StrictModel):
    """Config-backed scheduled job.

    ``workspace = "host"`` selects infrastructure host execution. Every other
    workspace selects the policy owner for an agent or deterministic job's
    derived child thread.
    """

    enabled: bool = True
    schedule: str | None = None
    interval_minutes: int | None = None
    at: str | None = None
    # NOTE: Update docs/usage/scheduled-tasks.md § Agent Tasks if target semantics change.
    workspace: str | None = None
    prompt: str | None = None
    prompt_file: str | None = None
    command: str | None = None
    cwd: str | None = None
    timeout_seconds: int | None = None
    quiet_on_success: bool | None = None
    display_name: str | None = None
    pre_run_command: str | None = None
    pre_run_cwd: str | None = None
    pre_run_timeout_seconds: int | None = None
    agent: bool = True
    reset_before_run: bool = True

    @property
    def is_host(self) -> bool:
        return self.workspace == "host"

    @property
    def is_deterministic(self) -> bool:
        return not self.is_host and not self.agent

    @field_validator("schedule")
    @classmethod
    def validate_job_cron(cls, v: str | None) -> str | None:
        if v is not None and not croniter.is_valid(v):
            msg = f"Invalid cron expression: {v}"
            raise ValueError(msg)
        return v

    @field_validator("at")
    @classmethod
    def validate_job_at(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            datetime.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(_JOB_AT_ERROR) from exc
        return v

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        if not value:
            raise ValueError("job workspace cannot be empty")
        return value

    @field_validator("command", "pre_run_command")
    @classmethod
    def validate_job_command(cls, v: str | None) -> str | None:
        if v is None:
            return None
        command = v.strip()
        if not command:
            raise ValueError(_JOB_COMMAND_EMPTY_ERROR)
        return command

    @field_validator("timeout_seconds", "pre_run_timeout_seconds", "interval_minutes")
    @classmethod
    def validate_job_timeout_seconds(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(_JOB_TIMEOUT_SECONDS_ERROR)
        return v

    @field_validator("display_name")
    @classmethod
    def validate_optional_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        if not value:
            raise ValueError("job text fields cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_job_shape(self) -> JobConfig:
        schedule_shapes = sum(
            value is not None for value in (self.schedule, self.interval_minutes, self.at)
        )
        if schedule_shapes != 1:
            raise ValueError(_JOB_SHAPE_ERROR)

        if self.is_host:
            return self._validate_host_shape()

        if self.workspace is None:
            raise ValueError(_AGENT_JOB_WORKSPACE_REQUIRED_ERROR)
        if self.is_deterministic:
            return self._validate_deterministic_shape()
        return self._validate_agent_shape()

    def _validate_host_shape(self) -> JobConfig:
        if self.command is None:
            raise ValueError(_HOST_JOB_COMMAND_ERROR)
        if self.schedule is None:
            raise ValueError(_HOST_JOB_SCHEDULE_ERROR)
        if self.prompt is not None or self.prompt_file is not None:
            raise ValueError(_HOST_JOB_PROMPT_ERROR)
        if any(
            value is not None
            for value in (
                self.pre_run_command,
                self.pre_run_cwd,
                self.pre_run_timeout_seconds,
            )
        ):
            raise ValueError(_HOST_JOB_PRE_RUN_ERROR)
        return self

    def _validate_deterministic_shape(self) -> JobConfig:
        if self.command is None:
            raise ValueError("deterministic workspace jobs require command")
        if self.prompt is not None or self.prompt_file is not None:
            raise ValueError("deterministic workspace jobs cannot set prompts")
        if any(
            value is not None
            for value in (
                self.pre_run_command,
                self.pre_run_cwd,
                self.pre_run_timeout_seconds,
            )
        ):
            raise ValueError("deterministic workspace jobs cannot set pre-run fields")
        return self

    def _validate_agent_shape(self) -> JobConfig:
        if self.command is not None:
            raise ValueError(_AGENT_JOB_COMMAND_ERROR)
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError(_AGENT_JOB_PROMPT_ERROR)
        if self.pre_run_command is None and (
            self.pre_run_cwd is not None or self.pre_run_timeout_seconds is not None
        ):
            raise ValueError("agent job pre-run options require pre_run_command")
        return self
