"""Deployment startup reconciliation regressions."""

import pytest

from pynchy.deployments import DeploymentState, DeployRevision
from pynchy.host.orchestrator.startup_handler import (
    InterruptedTurnRecovery,
    resolve_deploy_startup,
)
from pynchy.state import (
    get_deployment_state,
    get_router_state,
    init_test_database,
    initialize_deployment_state,
)


@pytest.mark.asyncio
async def test_stale_continuation_cannot_overwrite_active_release() -> None:
    await init_test_database()
    stale = DeployRevision("old-sha", "config-a")
    active = DeployRevision("new-sha", "config-b")
    await initialize_deployment_state(stale)
    recovery = InterruptedTurnRecovery(
        turns=(),
        commit_sha=stale.commit_sha,
        resume_prompt="",
        had_deploy_continuation=True,
        deploy_revision=stale,
        rolled_back=False,
        continuation_path=None,
    )

    await resolve_deploy_startup(recovery, active_revision=active)

    assert await get_deployment_state() == DeploymentState(applied=active, pending=None)
    assert await get_router_state("last_deploy_sha") == active.commit_sha
