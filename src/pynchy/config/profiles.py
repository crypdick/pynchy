"""Workspace profile config models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from pynchy.config.models import (
    ValidatedProfileName,
    ValidatedRepoSlug,
    ValidatedToolName,
    _StrictModel,
)
from pynchy.config.permissions import PermissionConfig


class CapabilityTomlConfig(_StrictModel):
    """Profile-level semantic capability policy for tool surfaces."""

    decision: Literal["allow", "deny", "needs_human"]


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
    capabilities: dict[str, CapabilityTomlConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_mixed_permission_syntax(self) -> ProfileConfig:
        if self.permissions.decisions and self.capabilities:
            raise ValueError("configure permissions or legacy capabilities, not both")
        return self

    @property
    def permission_decisions(self) -> dict[str, Literal["allow", "deny", "needs_human"]]:
        if self.permissions.decisions:
            return self.permissions.decisions
        return {capability: rule.decision for capability, rule in self.capabilities.items()}

    @field_validator("repo", mode="before")
    @classmethod
    def normalize_repo(cls, v: str | list[str] | None) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v
