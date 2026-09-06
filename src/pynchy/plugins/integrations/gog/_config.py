"""Host-owned configuration for the Gog Google Workspace integration."""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves Gog runtime callbacks at runtime.
)
from dataclasses import dataclass
from pathlib import (
    Path,  # noqa: TC003 - beartype resolves Gog runtime annotations at runtime.
)
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

PositiveTimeout = Annotated[float, Field(gt=0, le=300)]


class GogConfig(BaseModel):
    """Configuration kept on the host, never supplied by an agent request."""

    model_config = ConfigDict(extra="forbid")

    command: Annotated[str, Field(min_length=1)] = "gog"
    account: str | None = None
    home: str | None = None
    oauth_client_path: str | None = None
    timeout_seconds: PositiveTimeout = 60.0

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("command must be one executable path or command name")
        return normalized

    @field_validator("account")
    @classmethod
    def _validate_account(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or "\r" in normalized or "\n" in normalized:
            raise ValueError("account must be a single non-empty line")
        return normalized

    @field_validator("home", "oauth_client_path")
    @classmethod
    def _validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("path must be non-empty and cannot contain a NUL byte")
        return normalized


@dataclass(frozen=True)
class GogRuntime:
    """Resolved Gog configuration and workspace authorization."""

    config: GogConfig
    home: Path
    oauth_client_path: Path | None
    workspace_enables_gog: Callable[[str], bool]


_runtime: GogRuntime | None = None


def configure_gog_runtime(runtime: GogRuntime) -> None:
    """Set Gog configuration before host actions run."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def gog_runtime() -> GogRuntime:
    """Return the resolved Gog runtime or fail before processing a request."""
    if _runtime is None:
        raise RuntimeError("Gog runtime has not been configured")
    return _runtime
