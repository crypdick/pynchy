"""Semantic mount contracts owned by container orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import (
    Path,
)


@dataclass(frozen=True, slots=True)
class RepoMount:
    """One resolved repository worktree prepared for an agent container."""

    slug: str
    root: Path
    worktree_path: Path


@dataclass(frozen=True, slots=True)
class RepoMountResolution:
    """Prepared mounts and user-visible notices from source-control setup."""

    mounts: tuple[RepoMount, ...] = ()
    notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentHomeMounts:
    """Resolved agent-home paths needed by container mount assembly."""

    claude_home: Path
    codex_home: Path
    vault_mount_root: Path | None = None
    vault_mount_path: str | None = None
