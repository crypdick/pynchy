"""Plugin-owned configuration for one named Linear account."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class LinearTool(BaseModel):
    """One Linear credential plus its security-policy declarations."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["linear"]
    enabled: bool = True
    public_source: bool | Literal["forbidden"] = True
    secret_data: bool = True
    public_sink: bool | Literal["forbidden"] = True
    dangerous_writes: bool | Literal["forbidden"] = True
    workspace: str | None = None
    api_key_env: str = "LINEAR_API_KEY"
    team_key_env: str = "LINEAR_TEAM_KEY"
    project_per_workspace: bool | None = None
    project_name_template: str | None = None

    @field_validator("api_key_env", "team_key_env")
    @classmethod
    def validate_linear_env_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Linear credential environment names cannot be empty")
        return value
