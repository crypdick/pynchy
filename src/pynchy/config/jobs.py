"""Config-backed job models."""

from __future__ import annotations

from croniter import croniter
from pydantic import field_validator, model_validator

from pynchy.config.models import _StrictModel

_JOB_WORKSPACE_EMPTY_ERROR = "job workspace cannot be empty"
_JOB_COMMAND_EMPTY_ERROR = "host job command cannot be empty"
_JOB_TIMEOUT_SECONDS_ERROR = "timeout_seconds must be positive"
_JOB_SHAPE_ERROR = "jobs require exactly one of schedule or at"
_HOST_JOB_COMMAND_ERROR = "host jobs require command"
_HOST_JOB_SCHEDULE_ERROR = "host jobs require schedule"
_HOST_JOB_PROMPT_ERROR = "host jobs cannot set prompt or prompt_file"
_AGENT_JOB_COMMAND_ERROR = "agent jobs cannot set command"
_AGENT_JOB_PROMPT_ERROR = "agent jobs require prompt or prompt_file"


class JobConfig(_StrictModel):
    """Config-backed scheduled job.

    ``workspace = "host"`` selects host execution. Any other value targets a
    configured workspace and runs an agent with either ``prompt`` or
    ``prompt_file``.
    """

    enabled: bool = True
    schedule: str | None = None
    at: str | None = None
    workspace: str
    prompt: str | None = None
    prompt_file: str | None = None
    command: str | None = None
    cwd: str | None = None
    timeout_seconds: int | None = None
    quiet_on_success: bool | None = None

    @property
    def is_host(self) -> bool:
        return self.workspace == "host"

    @field_validator("schedule")
    @classmethod
    def validate_job_cron(cls, v: str | None) -> str | None:
        if v is not None and not croniter.is_valid(v):
            msg = f"Invalid cron expression: {v}"
            raise ValueError(msg)
        return v

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError(_JOB_WORKSPACE_EMPTY_ERROR)
        return value

    @field_validator("command")
    @classmethod
    def validate_job_command(cls, v: str | None) -> str | None:
        if v is None:
            return None
        command = v.strip()
        if not command:
            raise ValueError(_JOB_COMMAND_EMPTY_ERROR)
        return command

    @field_validator("timeout_seconds")
    @classmethod
    def validate_job_timeout_seconds(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(_JOB_TIMEOUT_SECONDS_ERROR)
        return v

    @model_validator(mode="after")
    def validate_job_shape(self) -> JobConfig:
        if (self.schedule is None) == (self.at is None):
            raise ValueError(_JOB_SHAPE_ERROR)

        if self.is_host:
            if self.command is None:
                raise ValueError(_HOST_JOB_COMMAND_ERROR)
            if self.schedule is None:
                raise ValueError(_HOST_JOB_SCHEDULE_ERROR)
            if self.prompt is not None or self.prompt_file is not None:
                raise ValueError(_HOST_JOB_PROMPT_ERROR)
            return self

        if self.command is not None:
            raise ValueError(_AGENT_JOB_COMMAND_ERROR)
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError(_AGENT_JOB_PROMPT_ERROR)
        return self
