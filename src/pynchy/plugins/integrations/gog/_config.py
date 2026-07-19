"""Host-owned configuration for the Gog Google Workspace integration."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pynchy.config import get_settings

if TYPE_CHECKING:
    from pynchy.config.settings import Settings

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

    def resolved_home(self, settings: Settings) -> Path:
        """Return Gog's private host state directory."""
        return _resolve_path(self.home, settings, default=settings.data_dir / "gog")

    def resolved_oauth_client_path(self, settings: Settings) -> Path | None:
        """Return the configured Desktop OAuth client file, if one was supplied."""
        if self.oauth_client_path is None:
            return None
        return _resolve_path(self.oauth_client_path, settings, default=settings.data_dir)


def gog_config() -> GogConfig:
    """Parse the plugin-owned options transport at the integration boundary."""
    plugin = get_settings().plugins.get("gog")
    return GogConfig.model_validate(plugin.options if plugin is not None else {})


def _resolve_path(value: str | None, settings: Settings, *, default: Path) -> Path:
    if value is None:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else settings.project_root / path
