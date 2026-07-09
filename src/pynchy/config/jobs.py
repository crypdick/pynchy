"""Config-backed job models."""

from __future__ import annotations

from croniter import croniter
from pydantic import field_validator, model_validator

from pynchy.config.models import _StrictModel


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
            raise ValueError("job workspace cannot be empty")
        return value

    @field_validator("command")
    @classmethod
    def validate_job_command(cls, v: str | None) -> str | None:
        if v is None:
            return None
        command = v.strip()
        if not command:
            raise ValueError("host job command cannot be empty")
        return command

    @field_validator("timeout_seconds")
    @classmethod
    def validate_job_timeout_seconds(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("timeout_seconds must be positive")
        return v

    @model_validator(mode="after")
    def validate_job_shape(self) -> JobConfig:
        if (self.schedule is None) == (self.at is None):
            raise ValueError("jobs require exactly one of schedule or at")

        if self.is_host:
            if self.command is None:
                raise ValueError("host jobs require command")
            if self.schedule is None:
                raise ValueError("host jobs require schedule")
            if self.prompt is not None or self.prompt_file is not None:
                raise ValueError("host jobs cannot set prompt or prompt_file")
            return self

        if self.command is not None:
            raise ValueError("agent jobs cannot set command")
        if (self.prompt is None) == (self.prompt_file is None):
            raise ValueError("agent jobs require prompt or prompt_file")
        return self
