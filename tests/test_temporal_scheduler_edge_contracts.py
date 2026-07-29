"""Behavior tests for Temporal scheduler control-plane edge contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
from pynchy.deployments import DeployChangeKind, DeployClaim, DeployClaimStatus
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.workflow_control import (
    TemporalRuntimeUnavailableError,
)
from tests.temporal_scheduler_support import NullSchedulerDeps, _scheduler_runtime


def _runtime() -> temporal_scheduler.TemporalSchedulerRuntime:
    return temporal_scheduler.TemporalSchedulerRuntime(NullSchedulerDeps(), _scheduler_runtime())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name",
    [
        "start_channel_reconciliation",
        "start_linear_work_item_reconciliation",
        "reconcile_schedules",
    ],
)
async def test_scheduler_operations_fail_closed_before_temporal_start(
    method_name: str,
) -> None:
    with pytest.raises(RuntimeError, match="has not been started"):
        await getattr(_runtime(), method_name)()


@pytest.mark.asyncio
async def test_public_scheduler_workflow_wrapper_reports_unavailable_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Loop:
        def __init__(self) -> None:
            self.calls = 0

        def time(self) -> float:
            self.calls += 1
            return 0.0 if self.calls == 1 else 11.0

    loop = _Loop()
    monkeypatch.setattr(temporal_scheduler.asyncio, "get_running_loop", lambda: loop)

    with pytest.raises(TemporalRuntimeUnavailableError, match="has not been started"):
        await temporal_scheduler.start_channel_reconciliation_workflow()


def test_publishing_scheduler_config_requires_an_active_runtime() -> None:
    with pytest.raises(TemporalRuntimeUnavailableError, match="has not been started"):
        temporal_scheduler.publish_scheduler_config(_scheduler_runtime())


@pytest.mark.asyncio
@pytest.mark.parametrize("cleared", [False, True])
async def test_terminal_scheduled_turn_reports_cleanup_result(
    monkeypatch: pytest.MonkeyPatch,
    cleared: bool,
) -> None:
    monkeypatch.setattr(
        temporal_scheduler,
        "clear_unclaimed_in_flight_turn_for_task",
        AsyncMock(return_value=cleared),
    )

    result = await temporal_scheduler.clear_terminal_scheduled_turn("task-1")

    assert result == ("cleared" if cleared else "preserved")


@pytest.mark.asyncio
async def test_disabled_canary_activity_does_not_invoke_external_checks() -> None:
    deps = NullSchedulerDeps()
    deps.scheduler_runtime = _scheduler_runtime()
    temporal_scheduler.bind_scheduler_deps(deps)
    try:
        result = await temporal_scheduler.run_scheduled_canaries()
    finally:
        temporal_scheduler.bind_scheduler_deps(None)

    assert result == "disabled"


@pytest.mark.asyncio
async def test_deploy_start_skips_already_admitted_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.client = Mock()
    monkeypatch.setattr(
        temporal_scheduler,
        "claim_deployment",
        AsyncMock(return_value=DeployClaim(DeployClaimStatus.ALREADY_PENDING)),
    )

    result = await runtime.start_deploy(
        DeployRequest(
            chat_jid="slack:admin",
            commit_sha="sha",
            config_hash="config",
            previous_sha="old",
        )
    )

    assert result.status is DeployClaimStatus.ALREADY_PENDING


@pytest.mark.asyncio
async def test_deploy_start_clears_claim_when_workflow_dispatch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    runtime.client = Mock()
    request = DeployRequest(
        chat_jid="slack:admin",
        commit_sha="sha",
        config_hash="config",
        previous_sha="old",
    )
    monkeypatch.setattr(
        temporal_scheduler,
        "claim_deployment",
        AsyncMock(return_value=DeployClaim(DeployClaimStatus.CLAIMED, DeployChangeKind.CODE)),
    )
    monkeypatch.setattr(
        runtime,
        "_start_workflow",
        AsyncMock(side_effect=RuntimeError("dispatch failed")),
    )
    clear_pending = AsyncMock()
    monkeypatch.setattr(temporal_scheduler, "clear_pending_deployment", clear_pending)

    with pytest.raises(RuntimeError, match="dispatch failed"):
        await runtime.start_deploy(request)

    clear_pending.assert_awaited_once_with(request.revision)


@pytest.mark.asyncio
async def test_scheduler_dispatch_records_generic_start_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    client = Mock()
    client.start_workflow = AsyncMock(side_effect=RuntimeError("Temporal offline"))
    runtime.client = client

    with pytest.raises(RuntimeError, match="Temporal offline"):
        await runtime.start_temporal_workflow(
            lambda: None,
            workflow_id="workflow-1",
            status_id="task-1",
        )

    assert temporal_scheduler.get_temporal_scheduler_status()["last_result"] == "error"
