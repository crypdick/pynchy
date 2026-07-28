"""Domain-level contracts shared by integration configuration and adapters."""

from __future__ import annotations

from typing import Literal, Protocol, TypeGuard, runtime_checkable

MatrixActivation = Literal["on_event", "on_demand"]
MatrixOutbound = Literal["read_only", "approval_required"]


@runtime_checkable
class LinearAccountConfig(Protocol):
    """Configured credential names consumed by the Linear adapter."""

    type: Literal["linear"]
    api_key_env: str
    team_key_env: str


def is_linear_account_config(value: object) -> TypeGuard[LinearAccountConfig]:
    """Recognize the validated Linear tool record at the adapter boundary."""
    return getattr(value, "type", None) == "linear"


@runtime_checkable
class MatrixRouteDefaults(Protocol):
    activation: MatrixActivation
    outbound: MatrixOutbound


@runtime_checkable
class MatrixEndpoint(Protocol):
    room_id: str
    title: str | None
    expected_bridge: str | None
    require_active_portal: bool


@runtime_checkable
class MatrixConnection(Protocol):
    type: Literal["matrix"]
    gateway_command_env: str
    expected_user_id: str
    poll_interval_seconds: float
    route_defaults: MatrixRouteDefaults
    chat: dict[str, MatrixEndpoint]


def is_matrix_connection(value: object) -> TypeGuard[MatrixConnection]:
    """Recognize the validated Matrix connection at the adapter boundary."""
    return getattr(value, "type", None) == "matrix"
