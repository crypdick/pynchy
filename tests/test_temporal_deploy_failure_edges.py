"""Focused public failure contracts for the Temporal deploy activity."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import pynchy.host.orchestrator.temporal.deploy as temporal_deploy
import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
from pynchy.deployments import DeployChangeKind
from pynchy.host.orchestrator.deploy import BuildResult, RollbackResult
from pynchy.host.orchestrator.temporal.runtime_state import TemporalActivityInfo
from pynchy.state import init_test_database
from tests.temporal_scheduler_support import NullSchedulerDeps


def _request(*, chat_jid: str = "slack:C123") -> temporal_deploy.DeployRequest:
    return temporal_deploy.DeployRequest(
        chat_jid=chat_jid,
        commit_sha="new-sha",
        config_hash="config-hash",
        previous_sha="old-sha",
        change_kind=DeployChangeKind.CODE,
    )


def _bind(deps: NullSchedulerDeps, monkeypatch: pytest.MonkeyPatch, workflow_id: str) -> None:
    monkeypatch.setattr(
        temporal_scheduler.activity,
        "info",
        lambda: TemporalActivityInfo(workflow_id=workflow_id),
    )
    temporal_scheduler.bind_scheduler_deps(deps)


def _raise_build(_root):
    raise RuntimeError("builder crashed")


@pytest.mark.asyncio
async def test_deploy_reports_rollback_failure_without_chat_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_test_database()
    deps = NullSchedulerDeps()
    deps.broadcast_host_message = AsyncMock()
    monkeypatch.setattr(
        temporal_deploy,
        "build_container_image",
        lambda _root: BuildResult(success=False, stderr="broken"),
    )
    monkeypatch.setattr(
        temporal_deploy,
        "rollback_deploy_checkout",
        lambda _sha: RollbackResult(success=False, error="checkout locked"),
    )
    _bind(deps, monkeypatch, "deploy-rollback-failed")

    result = await temporal_deploy.run_deploy(
        temporal_deploy.deploy_request_to_payload(_request(chat_jid=""))
    )

    assert result == "build_failed_rollback_failed"
    deps.broadcast_host_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_ignores_failure_notification_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    await init_test_database()
    deps = NullSchedulerDeps()
    deps.broadcast_host_message = AsyncMock(side_effect=RuntimeError("channel unavailable"))
    monkeypatch.setattr(
        temporal_deploy,
        "build_container_image",
        lambda _root: BuildResult(success=False, stderr="broken"),
    )
    monkeypatch.setattr(
        temporal_deploy,
        "rollback_deploy_checkout",
        lambda _sha: RollbackResult(success=True, actual_sha="old-sha-full"),
    )
    _bind(deps, monkeypatch, "deploy-notification-failed")

    result = await temporal_deploy.run_deploy(temporal_deploy.deploy_request_to_payload(_request()))

    assert result == "build_failed_rolled_back"
    deps.broadcast_host_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_deploy_rolls_back_when_container_build_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await init_test_database()
    deps = NullSchedulerDeps()
    deps.broadcast_host_message = AsyncMock()
    monkeypatch.setattr(
        temporal_deploy,
        "build_container_image",
        _raise_build,
    )
    monkeypatch.setattr(
        temporal_deploy,
        "rollback_deploy_checkout",
        lambda _sha: RollbackResult(success=True, actual_sha="old-sha-full"),
    )
    _bind(deps, monkeypatch, "deploy-build-exception")

    result = await temporal_deploy.run_deploy(temporal_deploy.deploy_request_to_payload(_request()))

    assert result == "build_failed_rolled_back"
    assert "builder crashed" in deps.broadcast_host_message.await_args.args[1]
