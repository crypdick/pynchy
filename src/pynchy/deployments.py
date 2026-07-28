"""Domain values for idempotent deployment admission."""

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


@dataclass(frozen=True)
class DeploymentState:
    """Canonical effective revisions applied by and pending for the host."""

    applied: DeployRevision | None
    pending: DeployRevision | None


class DeployClaimStatus(StrEnum):
    """Outcome of atomically admitting a requested deployment."""

    CLAIMED = "claimed"
    ALREADY_APPLIED = "already_applied"
    ALREADY_PENDING = "already_pending"
    BUSY = "busy"


@dataclass(frozen=True)
class DeployClaim:
    """Deploy admission result with a cause only when work was claimed."""

    status: DeployClaimStatus
    change_kind: DeployChangeKind | None = None
