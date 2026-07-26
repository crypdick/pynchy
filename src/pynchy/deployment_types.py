"""Deployment revision and change-kind domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class DeployRevision:
    """Effective host revision whose equality makes deploys idempotent."""

    commit_sha: str
    config_hash: str


class DeployChangeKind(StrEnum):
    """User-facing reason that an effective host revision changed."""

    CODE = "code change"
    CONFIG = "config change"
    CODE_AND_CONFIG = "code and config changes"
    RESTART = "restart request"

    @classmethod
    def between(
        cls,
        applied: DeployRevision | None,
        target: DeployRevision,
    ) -> DeployChangeKind:
        """Describe the semantic difference between two deploy revisions."""
        if applied is None:
            return cls.CODE_AND_CONFIG
        code_changed = applied.commit_sha != target.commit_sha
        config_changed = applied.config_hash != target.config_hash
        if code_changed and config_changed:
            return cls.CODE_AND_CONFIG
        if code_changed:
            return cls.CODE
        if config_changed:
            return cls.CONFIG
        return cls.RESTART
