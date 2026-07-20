"""Plugin-owned configuration for routed Matrix conversations."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MatrixActivation = Literal["on_event", "on_demand"]
MatrixOutbound = Literal["read_only", "approval_required"]
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_ROOM_ID = re.compile(r"!\S+:\S+")
_USER_ID = re.compile(r"@\S+:\S+")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MatrixRouteDefaults(_StrictModel):
    """Connection-level defaults inherited by exact routes."""

    activation: MatrixActivation = "on_demand"
    outbound: MatrixOutbound = "approval_required"


class MatrixEndpointConfig(_StrictModel):
    """One immutable Matrix room endpoint with optional bridge assertions."""

    room_id: str = Field(min_length=1)
    title: str | None = Field(default=None, min_length=1)
    expected_bridge: str | None = Field(default=None, min_length=1)
    require_active_portal: bool = False
    enabled: bool = True

    @field_validator("room_id")
    @classmethod
    def validate_room_id(cls, value: str) -> str:
        if not _ROOM_ID.fullmatch(value):
            raise ValueError("Matrix endpoint room_id must be an immutable Matrix room ID")
        return value


class MatrixConnectionConfig(_StrictModel):
    """One authenticated Matrix owner identity and its named endpoints."""

    type: Literal["matrix"] = "matrix"
    gateway_command_env: str = "PYNCHY_MATRIX_GATEWAY"
    expected_user_id: str = Field(min_length=1)
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=300)
    route_defaults: MatrixRouteDefaults = Field(default_factory=MatrixRouteDefaults)
    chat: dict[str, MatrixEndpointConfig] = Field(default_factory=dict)

    @field_validator("gateway_command_env")
    @classmethod
    def validate_gateway_command_env(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("gateway_command_env must name an environment variable")
        return value

    @field_validator("expected_user_id")
    @classmethod
    def validate_expected_user_id(cls, value: str) -> str:
        if not _USER_ID.fullmatch(value):
            raise ValueError("expected_user_id must be a full Matrix user ID")
        return value
