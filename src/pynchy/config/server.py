"""Fail-closed HTTP control-plane configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, field_validator


class ServerConfig(BaseModel):
    """Listener, authentication, and rate-limit settings for the control plane."""

    model_config = {"extra": "forbid"}

    # NOTE: Update docs/usage/control-plane.md and docs/installation/server.md (§ Headless
    # Server Deployment) if these listener or authentication defaults change.
    host: str = "127.0.0.1"
    port: int = 8484
    unix_socket: Path | None = Path("data/pynchy.sock")
    allow_public_bind: bool = False
    allow_remote_deploy: bool = False
    auth_token_env: str = "PYNCHY_CONTROL_TOKEN"  # noqa: S105, RUF100 - environment variable name, not a credential value.
    auth_token_file: Path | None = Path("data/control-plane.token")
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60

    @field_validator("port")
    @classmethod
    def validate_server_port(cls, v: int) -> int:
        if not 1 <= v <= 65535:
            raise ValueError("server.port must be between 1 and 65535")
        return v

    @field_validator("rate_limit_requests", "rate_limit_window_seconds")
    @classmethod
    def validate_positive_server_values(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("server rate-limit values must be positive")
        return v

    @field_validator("host", "auth_token_env")
    @classmethod
    def validate_nonempty_server_values(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("server host and auth_token_env must not be empty")
        return v
