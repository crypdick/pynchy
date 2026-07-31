"""Behavior tests for Temporal scheduler control-plane edge contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
from pynchy.config.api import CanaryConfig
from pynchy.deployments import DeployChangeKind, DeployClaim, DeployClaimStatus
from pynchy.host.orchestrator.temporal.deploy import DeployRequest
from pynchy.host.orchestrator.temporal.workflow_control import (
    TemporalRuntimeUnavailableError,
)
from pynchy.learning_packets import LearningPacket
from pynchy.linear_plan_types import LinearPlanReviewAdmission
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


@pytest.mark.asyncio
async def test_scheduler_waits_briefly_for_runtime_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Loop:
        def __init__(self) -> None:
            self.calls = 0

        def time(self) -> float:
            self.calls += 1
            return 0.0 if self.calls < 3 else 11.0

    loop = _Loop()
    sleep = AsyncMock()
    monkeypatch.setattr(temporal_scheduler.asyncio, "get_running_loop", lambda: loop)
    monkeypatch.setattr(temporal_scheduler.asyncio, "sleep", sleep)

    with pytest.raises(TemporalRuntimeUnavailableError, match="has not been started"):
        await temporal_scheduler.start_channel_reconciliation_workflow()

    sleep.assert_awaited_once_with(0.05)


def _learning_packet() -> LearningPacket:
    return LearningPacket(
        job_id="job-1",
        chat_jid="slack:admin",
        group_folder="research",
        profile="default",
        created_at="2026-07-29T00:00:00Z",
        messages=[],
        final_answer=None,
        tool_counts={},
        error_snippets=[],
        loaded_skills=[],
        provenance={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "args"),
    [
        ("reconcile_schedules_with_config", (_scheduler_runtime(),)),
        ("start_learning_review_workflow", (_learning_packet(),)),
        ("start_interactive_message_workflow", ("slack:admin",)),
        ("start_interrupted_turn_workflow", ("turn-1", "research")),
        (
            "start_deploy_workflow",
            (
                DeployRequest(
                    chat_jid="slack:admin",
                    commit_sha="sha",
                    config_hash="config",
                    previous_sha="old",
                ),
            ),
        ),
        (
            "start_linear_plan_review_workflow",
            (LinearPlanReviewAdmission("research", "issue-1", "P-1", "now", True),),
        ),
    ],
)
async def test_scheduler_workflow_wrappers_propagate_runtime_unavailability(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    args: tuple[object, ...],
) -> None:
    monkeypatch.setattr(
        "pynchy.host.orchestrator.temporal.scheduler._require_active_runtime",
        AsyncMock(side_effect=TemporalRuntimeUnavailableError("unavailable")),
    )

    with pytest.raises(TemporalRuntimeUnavailableError, match="unavailable"):
        await getattr(temporal_scheduler, operation)(*args)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("start_learning_review", (_learning_packet(),)),
        ("start_interactive_message_turn", ("slack:admin",)),
        ("start_interrupted_turn", ("turn-1", "research")),
        (
            "start_deploy",
            (
                DeployRequest(
                    chat_jid="slack:admin",
                    commit_sha="sha",
                    config_hash="config",
                    previous_sha="old",
                ),
            ),
        ),
    ],
)
async def test_scheduler_start_methods_fail_closed_without_client(
    method_name: str,
    args: tuple[object, ...],
) -> None:
    with pytest.raises(RuntimeError, match="has not been started"):
        await getattr(_runtime(), method_name)(*args)


def test_publishing_scheduler_config_requires_an_active_runtime() -> None:
    with pytest.raises(TemporalRuntimeUnavailableError, match="has not been started"):
        temporal_scheduler.publish_scheduler_config(_scheduler_runtime())


@pytest.mark.asyncio
async def test_publishing_scheduler_config_updates_the_active_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    config = _scheduler_runtime()
    monkeypatch.setattr(
        temporal_scheduler.Client,
        "connect",
        AsyncMock(return_value=Mock()),
    )

    class _WorkerContext:
        async def __aenter__(self) -> _WorkerContext:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(temporal_scheduler, "Worker", lambda *_args, **_kwargs: _WorkerContext())

    async with runtime:
        temporal_scheduler.publish_scheduler_config(config)

    assert runtime.scheduler_config is config


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
async def test_successful_canary_run_without_transitions_stays_quiet() -> None:
    deps = NullSchedulerDeps()
    deps.scheduler_runtime = _scheduler_runtime(
        canary=CanaryConfig(
            enabled=True,
            target_profile="external-canary",
            scenario_ids=["calendar.round.trip"],
        )
    )
    deps.run_declared_canaries = AsyncMock(return_value=[])
    deps.broadcast_host_message = AsyncMock()
    temporal_scheduler.bind_scheduler_deps(deps)
    try:
        result = await temporal_scheduler.run_scheduled_canaries()
    finally:
        temporal_scheduler.bind_scheduler_deps(None)

    assert result == "completed:0"
    deps.run_declared_canaries.assert_awaited_once_with("external-canary", ("calendar.round.trip",))
    deps.broadcast_host_message.assert_not_awaited()


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
