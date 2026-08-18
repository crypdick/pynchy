"""Workspace profile config models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from pynchy.config.models import (
    ValidatedProfileName,
    ValidatedRepoSlug,
    ValidatedToolName,
    _StrictModel,
)
from pynchy.config.permissions import PermissionConfig


class ProfileConfig(_StrictModel):
    """Composable workspace profile config."""

    includes: list[ValidatedProfileName] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    denied_skills: list[str] = Field(default_factory=list)
    tools: list[ValidatedToolName] = Field(default_factory=list)
    repo: list[ValidatedRepoSlug] = Field(default_factory=list)
    model: str | None = None
    execution_mode: Literal["container", "host"] | None = None
    cwd: str | None = None
    is_admin: bool = False
    contains_secrets: bool = False
    cop_active: bool | None = None
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)

    @field_validator("repo", mode="before")
    @classmethod
    def normalize_repo(cls, v: str | list[str] | None) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v
