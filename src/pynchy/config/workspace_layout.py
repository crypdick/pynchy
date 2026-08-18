"""Declarative child-thread and source-workspace configuration."""

from __future__ import annotations

from collections.abc import (
    Mapping,  # noqa: TC003 - config validation annotations resolve at runtime.
)
from typing import Any, Literal, cast

from pydantic import BaseModel, field_validator, model_validator

from pynchy.config.permissions import PermissionConfig


class _StrictWorkspaceLayoutModel(BaseModel):
    model_config = {"extra": "forbid"}


type _CodexModelReasoningEffort = Literal["low", "medium", "high", "xhigh", "max", "ultra"]


class WorkspaceThreadConfig(_StrictWorkspaceLayoutModel):
    """A durable child conversation declared below one workspace root.

    ``workspace`` turns the thread into a semantic workspace with its own
    profiles.  The Discord thread remains physically below the declaring root,
    while jobs and routed conversations use the semantic workspace as their
    policy owner.
    """

    name: str
    # NOTE: Update docs/usage/workspaces.md § Organize Child Conversations if
    # these kinds change.
    kind: Literal["automation", "planning", "testing", "topic"] = "topic"
    workspace: str | None = None
    profiles: list[str] = []
    permissions: PermissionConfig = PermissionConfig()
    soul: str | None = None
    pipeline: str | None = None
    model: str | None = None
    model_reasoning_effort: _CodexModelReasoningEffort | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("workspace thread name cannot be empty")
        return name

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: str | None) -> str | None:
        if value is None:
            return None
        workspace = value.strip()
        if not workspace:
            raise ValueError("workspace thread workspace cannot be empty")
        return workspace

    @field_validator("soul")
    @classmethod
    def validate_soul(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("souls/"):
            raise ValueError("workspace soul must use the souls/ scope")
        return value

    @model_validator(mode="after")
    def validate_semantic_workspace_shape(self) -> WorkspaceThreadConfig:
        if self.workspace is None and (
            self.profiles
            or self.permissions.decisions
            or self.soul is not None
            or self.pipeline is not None
            or self.model is not None
            or self.model_reasoning_effort is not None
        ):
            raise ValueError("workspace thread policy fields require workspace")
        if self.workspace is not None and not self.profiles:
            raise ValueError("semantic workspace threads require profiles")
        return self


class WorkspaceScopeConfig(_StrictWorkspaceLayoutModel):
    """A policy owner placed below a physical root without a static control."""

    workspace: str
    profiles: list[str]
    permissions: PermissionConfig = PermissionConfig()
    soul: str | None = None
    pipeline: str | None = None
    model: str | None = None
    model_reasoning_effort: _CodexModelReasoningEffort | None = None

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, value: str) -> str:
        workspace = value.strip()
        if not workspace:
            raise ValueError("workspace scope cannot be empty")
        return workspace

    @field_validator("profiles")
    @classmethod
    def validate_profiles(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("workspace scopes require profiles")
        return value

    @field_validator("soul")
    @classmethod
    def validate_soul(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("souls/"):
            raise ValueError("workspace soul must use the souls/ scope")
        return value


def semantic_workspace_configs(
    workspaces: Mapping[str, Any],
) -> dict[str, tuple[str, WorkspaceThreadConfig | WorkspaceScopeConfig]]:
    """Return semantic child workspace definitions keyed by policy identity."""
    result: dict[str, tuple[str, WorkspaceThreadConfig | WorkspaceScopeConfig]] = {}
    for parent, config in workspaces.items():
        semantic = [*config.scopes, *(thread for thread in config.threads if thread.workspace)]
        for child in semantic:
            workspace = cast("str", child.workspace)
            if workspace in workspaces:
                raise ValueError(
                    f"semantic workspace {workspace!r} conflicts with a root workspace"
                )
            if workspace in result:
                raise ValueError(f"semantic workspace {workspace!r} is declared more than once")
            result[workspace] = (parent, child)
    return result
