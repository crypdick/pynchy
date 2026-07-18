"""Tests for canonical deployment admission and revision state."""

from __future__ import annotations

import pytest

from pynchy.state import (
    advance_deployment_baseline,
    claim_deployment,
    clear_pending_deployment,
    complete_deployment,
    get_deployment_state,
    init_test_database,
    initialize_deployment_state,
)
from pynchy.types import (
    DeployChangeKind,
    DeployClaimStatus,
    DeploymentState,
    DeployRevision,
)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


async def test_duplicate_revision_is_rejected_while_pending_and_after_boot() -> None:
    """One effective revision can cause at most one successful restart."""
    applied = DeployRevision("old-sha", "config-a")
    target = DeployRevision("new-sha", "config-a")
    await initialize_deployment_state(applied)

    first = await claim_deployment(target)
    while_pending = await claim_deployment(target)

    assert first.status is DeployClaimStatus.CLAIMED
    assert first.change_kind is DeployChangeKind.CODE
    assert while_pending.status is DeployClaimStatus.ALREADY_PENDING
    assert (await get_deployment_state()).pending == target

    await complete_deployment(target)

    after_boot = await claim_deployment(target)
    assert after_boot.status is DeployClaimStatus.ALREADY_APPLIED
    assert await get_deployment_state() == DeploymentState(
        applied=target,
        pending=None,
    )


@pytest.mark.parametrize(
    ("target", "expected_kind"),
    [
        (DeployRevision("old-sha", "config-b"), DeployChangeKind.CONFIG),
        (DeployRevision("new-sha", "config-b"), DeployChangeKind.CODE_AND_CONFIG),
    ],
)
async def test_claim_describes_the_effective_revision_change(
    target: DeployRevision,
    expected_kind: DeployChangeKind,
) -> None:
    await initialize_deployment_state(DeployRevision("old-sha", "config-a"))

    claim = await claim_deployment(target)

    assert claim.status is DeployClaimStatus.CLAIMED
    assert claim.change_kind is expected_kind


async def test_failed_revision_can_be_retried_after_its_claim_is_released() -> None:
    """A failed attempt is not permanently mistaken for an applied deploy."""
    target = DeployRevision("new-sha", "config-a")
    await initialize_deployment_state(DeployRevision("old-sha", "config-a"))
    await claim_deployment(target)

    await clear_pending_deployment(target)
    retry = await claim_deployment(target)

    assert retry.status is DeployClaimStatus.CLAIMED
    assert retry.change_kind is DeployChangeKind.CODE


async def test_different_revision_waits_while_a_deploy_is_pending() -> None:
    await initialize_deployment_state(DeployRevision("old-sha", "config-a"))
    await claim_deployment(DeployRevision("new-sha", "config-a"))

    claim = await claim_deployment(DeployRevision("newer-sha", "config-b"))

    assert claim.status is DeployClaimStatus.BUSY


async def test_forced_redeploy_preserves_explicit_restart_requests() -> None:
    """Manual redeploy remains available without weakening automatic dedupe."""
    revision = DeployRevision("current-sha", "config-a")
    await initialize_deployment_state(revision)

    claim = await claim_deployment(revision, force=True)

    assert claim.status is DeployClaimStatus.CLAIMED
    assert claim.change_kind is DeployChangeKind.RESTART


async def test_idle_sync_cannot_clear_a_pending_forced_restart() -> None:
    """A no-op sync poll must not reopen admission while restart work is active."""
    revision = DeployRevision("current-sha", "config-a")
    await initialize_deployment_state(revision)
    await claim_deployment(revision, force=True)

    await advance_deployment_baseline(revision)

    assert await get_deployment_state() == DeploymentState(
        applied=revision,
        pending=revision,
    )
