"""Declarative child-thread and source-workspace migration configuration."""

from __future__ import annotations

from collections.abc import (
    Mapping,  # noqa: TC003, RUF100 - config validation annotations resolve at runtime.
)
from typing import Any

from pydantic import BaseModel, field_validator, model_validator


class _StrictWorkspaceLayoutModel(BaseModel):
    model_config = {"extra": "forbid"}


class WorkspaceThreadConfig(_StrictWorkspaceLayoutModel):
    """A durable child conversation declared below one workspace root.

    ``workspace`` turns the thread into a semantic workspace with its own
    profiles.  The Discord thread remains physically below the declaring root,
    while jobs and routed conversations use the semantic workspace as their
    policy owner.
    """

    name: str
    workspace: str | None = None
    profiles: list[str] = []
    model: str | None = None

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

    @model_validator(mode="after")
    def validate_semantic_workspace_shape(self) -> WorkspaceThreadConfig:
        if self.workspace is None and (self.profiles or self.model is not None):
            raise ValueError("workspace thread profiles and model require workspace")
        if self.workspace is not None and not self.profiles:
            raise ValueError("semantic workspace threads require profiles")
        return self


class WorkspaceScopeConfig(_StrictWorkspaceLayoutModel):
    """A policy owner placed below a physical root without a static control."""

    workspace: str
    profiles: list[str]
    model: str | None = None

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


def semantic_workspace_configs(
    workspaces: Mapping[str, Any],
) -> dict[str, tuple[str, WorkspaceThreadConfig | WorkspaceScopeConfig]]:
    """Return semantic child workspace definitions keyed by policy identity."""
    result: dict[str, tuple[str, WorkspaceThreadConfig | WorkspaceScopeConfig]] = {}
    for parent, config in workspaces.items():
        semantic = [*config.scopes, *(thread for thread in config.threads if thread.workspace)]
        for child in semantic:
            workspace = child.workspace
            if workspace is None:
                continue
            if workspace in workspaces:
                raise ValueError(
                    f"semantic workspace {workspace!r} conflicts with a root workspace"
                )
            if workspace in result:
                raise ValueError(f"semantic workspace {workspace!r} is declared more than once")
            result[workspace] = (parent, child)
    return result


class WorkspaceMigrationConfig(_StrictWorkspaceLayoutModel):
    """Explicit retirement gate for one source root workspace."""

    target_workspace: str
    target_thread: str
    inbound_retargeted: bool = False
    scheduled_jobs_retargeted: bool = False
    retire_legacy_workspace: bool = False

    @field_validator("target_workspace", "target_thread")
    @classmethod
    def validate_target_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("workspace migration targets cannot be empty")
        return name

    @model_validator(mode="after")
    def validate_retirement_confirmation(self) -> WorkspaceMigrationConfig:
        if self.retire_legacy_workspace and not (
            self.inbound_retargeted and self.scheduled_jobs_retargeted
        ):
            raise ValueError(
                "retire_legacy_workspace requires inbound_retargeted and scheduled_jobs_retargeted"
            )
        return self


def validate_workspace_migrations(
    migrations: Mapping[str, WorkspaceMigrationConfig],
    workspaces: Mapping[str, Any],
) -> None:
    """Ensure source-workspace retirement records name declared destinations."""
    for source_workspace, migration in migrations.items():
        if source_workspace == migration.target_workspace:
            raise ValueError("workspace migration cannot target the same workspace")
        target = workspaces.get(migration.target_workspace)
        if target is None:
            raise ValueError(
                f"workspace migration {source_workspace!r} targets unknown workspace "
                f"{migration.target_workspace!r}"
            )
        if migration.target_thread.casefold() not in {
            thread.name.casefold() for thread in target.threads
        }:
            raise ValueError(
                f"workspace migration {source_workspace!r} targets undeclared thread "
                f"{migration.target_thread!r} in {migration.target_workspace!r}"
            )
