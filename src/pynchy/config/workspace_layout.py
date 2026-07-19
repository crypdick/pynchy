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
    """A durable child conversation declared below one workspace root."""

    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("workspace thread name cannot be empty")
        return name


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
