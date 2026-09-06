"""Configuration and failure types for local Proton Mail Bridge access."""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_BRIDGE_IMAP_PORT = 1143
_BRIDGE_SMTP_PORT = 1025
_PASSWORD_COMMAND_ENV = "PYNCHY_PROTON_BRIDGE_PASSWORD_COMMAND"  # noqa: S105  # pragma: allowlist secret
_USERNAME_ENV = "PYNCHY_PROTON_BRIDGE_USERNAME"
_IMAP_PORT_ENV = "PYNCHY_PROTON_BRIDGE_IMAP_PORT"
_SMTP_PORT_ENV = "PYNCHY_PROTON_BRIDGE_SMTP_PORT"


class ProtonMailError(RuntimeError):
    """Raised when the local Proton Bridge integration cannot complete an operation."""


class ProtonBridgeConfiguration(BaseModel):
    """Connection and credential-command configuration for local Bridge mail protocols."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(min_length=1)
    password_command: str = Field(min_length=1)
    imap_port: int = Field(default=_BRIDGE_IMAP_PORT, ge=1, le=65535)
    smtp_port: int = Field(default=_BRIDGE_SMTP_PORT, ge=1, le=65535)

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("username must be a single non-empty line")
        return normalized

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> ProtonBridgeConfiguration:
        """Load the deliberately narrow Bridge configuration from an MCP environment."""
        source = os.environ if environment is None else environment
        values: dict[str, str | None] = {
            "username": source.get(_USERNAME_ENV),
            "password_command": source.get(_PASSWORD_COMMAND_ENV),
        }
        for field_name, environment_name in (
            ("imap_port", _IMAP_PORT_ENV),
            ("smtp_port", _SMTP_PORT_ENV),
        ):
            value = source.get(environment_name)
            if value is not None:
                values[field_name] = value
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ProtonMailError(
                f"Configure {_USERNAME_ENV} and {_PASSWORD_COMMAND_ENV} for Proton Bridge"
            ) from exc
