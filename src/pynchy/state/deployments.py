"""Canonical deployment admission and applied-revision state."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from pynchy.deployments import (
    DeployChangeKind,
    DeployClaim,
    DeployClaimStatus,
    DeploymentState,
    DeployRevision,
)
from pynchy.state.connection import _get_db, atomic_write

_SINGLETON = 1


def _revision_from_row(
    row: aiosqlite.Row | None,
    prefix: str,
) -> DeployRevision | None:
    if row is None:
        return None
    commit_sha = row[f"{prefix}_sha"]
    config_hash = row[f"{prefix}_config_hash"]
    if not commit_sha or not config_hash:
        return None
    return DeployRevision(commit_sha=str(commit_sha), config_hash=str(config_hash))


async def _get_row(db: aiosqlite.Connection) -> aiosqlite.Row | None:
    cursor = await db.execute(
        "SELECT applied_sha, applied_config_hash, pending_sha, pending_config_hash "
        "FROM deployment_state WHERE singleton = ?",
        (_SINGLETON,),
    )
    return await cursor.fetchone()


async def get_deployment_state() -> DeploymentState:
    """Return the canonical applied and pending effective revisions."""
    row = await _get_row(_get_db())
    return DeploymentState(
        applied=_revision_from_row(row, "applied"),
        pending=_revision_from_row(row, "pending"),
    )


async def initialize_deployment_state(revision: DeployRevision) -> None:
    """Seed an existing running host without overwriting durable deploy state."""
    async with atomic_write() as db:
        await db.execute(
            "INSERT OR IGNORE INTO deployment_state "
            "(singleton, applied_sha, applied_config_hash) VALUES (?, ?, ?)",
            (_SINGLETON, revision.commit_sha, revision.config_hash),
        )
        await db.execute(
            "UPDATE deployment_state SET applied_sha = ?, applied_config_hash = ? "
            "WHERE singleton = ? AND applied_sha IS NULL AND pending_sha IS NULL",
            (revision.commit_sha, revision.config_hash, _SINGLETON),
        )


async def claim_deployment(
    revision: DeployRevision,
    *,
    force: bool = False,
) -> DeployClaim:
    """Atomically admit one effective revision across every deploy trigger."""
    async with atomic_write() as db:
        await db.execute(
            "INSERT OR IGNORE INTO deployment_state (singleton) VALUES (?)",
            (_SINGLETON,),
        )
        row = await _get_row(db)
        applied = _revision_from_row(row, "applied")
        pending = _revision_from_row(row, "pending")
        if pending == revision:
            return DeployClaim(DeployClaimStatus.ALREADY_PENDING)
        if pending is not None:
            return DeployClaim(DeployClaimStatus.BUSY)
        if applied == revision and not force:
            return DeployClaim(DeployClaimStatus.ALREADY_APPLIED)

        await db.execute(
            "UPDATE deployment_state SET pending_sha = ?, pending_config_hash = ? "
            "WHERE singleton = ?",
            (revision.commit_sha, revision.config_hash, _SINGLETON),
        )
        return DeployClaim(
            DeployClaimStatus.CLAIMED,
            DeployChangeKind.between(applied, revision),
        )


async def clear_pending_deployment(revision: DeployRevision) -> None:
    """Release a failed deploy claim without disturbing a newer request."""
    async with atomic_write() as db:
        await db.execute(
            "UPDATE deployment_state SET pending_sha = NULL, pending_config_hash = NULL "
            "WHERE singleton = ? AND pending_sha = ? AND pending_config_hash = ?",
            (_SINGLETON, revision.commit_sha, revision.config_hash),
        )


async def advance_deployment_baseline(revision: DeployRevision) -> None:
    """Advance the applied revision when a checkout change needs no restart."""
    async with atomic_write() as db:
        await db.execute(
            "INSERT OR IGNORE INTO deployment_state (singleton) VALUES (?)",
            (_SINGLETON,),
        )
        await db.execute(
            "UPDATE deployment_state SET applied_sha = ?, applied_config_hash = ? "
            "WHERE singleton = ? AND pending_sha IS NULL AND pending_config_hash IS NULL",
            (revision.commit_sha, revision.config_hash, _SINGLETON),
        )


async def complete_deployment(revision: DeployRevision) -> None:
    """Promote a successfully booted revision and update operator metadata."""
    async with atomic_write() as db:
        await _write_applied_revision(db, revision)
        deployed_at = datetime.now(UTC).isoformat()
        for key, value in {
            "last_deploy_at": deployed_at,
            "last_deploy_sha": revision.commit_sha,
        }.items():
            await db.execute(
                "INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)",
                (key, value),
            )


async def _write_applied_revision(
    db: aiosqlite.Connection,
    revision: DeployRevision,
) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO deployment_state (singleton) VALUES (?)",
        (_SINGLETON,),
    )
    await db.execute(
        "UPDATE deployment_state "
        "SET applied_sha = ?, applied_config_hash = ?, "
        "pending_sha = CASE WHEN pending_sha = ? AND pending_config_hash = ? THEN NULL "
        "ELSE pending_sha END, "
        "pending_config_hash = CASE "
        "WHEN pending_sha = ? AND pending_config_hash = ? THEN NULL "
        "ELSE pending_config_hash END "
        "WHERE singleton = ?",
        (
            revision.commit_sha,
            revision.config_hash,
            revision.commit_sha,
            revision.config_hash,
            revision.commit_sha,
            revision.config_hash,
            _SINGLETON,
        ),
    )
