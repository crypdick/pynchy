"""Shared result and error types for git worktree operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


class WorktreeError(Exception):
    """Failed to create or sync a git worktree."""


class RoutedHostWorktreeError(WorktreeError):
    """A routed host conversation cannot safely use an isolated worktree."""


@dataclass
class WorktreeResult:
    """Result of worktree provisioning and its agent-facing notices."""

    path: Path
    notices: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoutedHostWorktreeResult:
    """Resolved host CWD and notices for one routed conversation."""

    cwd: Path
    notices: tuple[str, ...]
    repo_access: str
