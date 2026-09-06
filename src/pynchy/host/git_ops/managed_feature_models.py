"""Trusted managed-feature publication value objects."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import (
    Path,
)

from pynchy.host.git_ops.repo import (
    RepoContext,
)


@dataclass(frozen=True)
class ManagedFeaturePublication:
    """Host-validated identity of one managed feature branch."""

    repo_ctx: RepoContext
    feature_slug: str
    worktree_path: Path
    branch_name: str
    main_branch: str
    remote_url: str
    base_sha: str
    head_sha: str
    object_format: str
    ahead: int
    git_common_dir: Path

    @property
    def repo_slug(self) -> str:
        """Return the configured repository identity for Cop summaries."""
        return self.repo_ctx.slug

    def binding(self) -> dict[str, str]:
        """Return fields that must remain unchanged through publication."""
        return {
            "feature_slug": self.feature_slug,
            "repository": self.repo_slug,
            "branch": self.branch_name,
            "target_branch": self.main_branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
        }


@dataclass(frozen=True)
class ManagedFeatureResolution:
    """Managed-feature resolution outcome safe to return to a lifecycle handler."""

    publication: ManagedFeaturePublication | None
    error: str | None
