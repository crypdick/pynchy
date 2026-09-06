"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

import pynchy.host.orchestrator.temporal.interactive as temporal_interactive
import pynchy.host.orchestrator.temporal.interrupted as temporal_interrupted
import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
import pynchy.host.orchestrator.temporal.schedules as temporal_schedules
import pynchy.host.orchestrator.temporal.workflows as temporal_workflows
from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.config.api import (
    SchedulerConfig,
)
from pynchy.deployments import (
    DeployChangeKind,
)
from pynchy.host.orchestrator.startup_readiness import (
    StartupReadiness,
    StartupReadinessError,
)
from pynchy.host.orchestrator.temporal.runtime_state import TemporalActivityInfo
from pynchy.learning_packets import packet_to_payload
from pynchy.state import (
    begin_in_flight_turn,
    get_in_flight_turn,
    init_test_database,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile
from tests.temporal_scheduler_support import (
    FakeScheduleClient,
    NullSchedulerDeps,
    _scheduler_runtime,
)

pytest_plugins = ("tests.temporal_scheduler_support",)

TEMPORAL_UNAVAILABLE_MESSAGE = "temporal unavailable"
PAUSED_TASK_RUN_MESSAGE = "paused tasks must not run"


class TestTemporalSchedulerRuntime:
    @staticmethod
    def _capturing_worker(captured: dict[str, object]):
        @asynccontextmanager
        async def fake_worker(*args, workflows, activities, **kwargs):
            captured["workflows"] = workflows
            captured["activities"] = activities
            yield object()

        return fake_worker

    @staticmethod
    def _assert_registered_temporal_workflows(
        captured: dict[str, object], temporal_scheduler
    ) -> None:
        assert {
            temporal_workflows.InteractiveMessageWorkflow,
            temporal_workflows.InterruptedTurnWorkflow,
            temporal_workflows.LearningReviewWorkflow,
            temporal_workflows.DeployWorkflow,
            temporal_workflows.HostGitSyncWorkflow,
            temporal_workflows.ExternalGitSyncWorkflow,
            temporal_workflows.ChannelReconciliationWorkflow,
            temporal_workflows.LinearWorkItemReconciliationWorkflow,
        }.issubset(set(captured["workflows"]))
        assert {
            temporal_scheduler.run_interactive_message_turn,
            temporal_scheduler.run_interactive_runtime_turn,
            temporal_scheduler.run_interrupted_agent_turn,
            temporal_scheduler.clear_terminal_scheduled_turn,
            temporal_scheduler.run_learning_review,
            temporal_scheduler.run_deploy,
            temporal_scheduler.run_host_git_sync,
            temporal_scheduler.run_external_git_sync,
            temporal_scheduler.run_channel_reconciliation,
            temporal_scheduler.run_linear_work_item_reconciliation,
        }.issubset(set(captured["activities"]))

    @staticmethod
    def _assert_finalize_deploy_kwargs(finalize_deploy: AsyncMock, deps: NullSchedulerDeps) -> None:
        finalize_deploy.assert_awaited_once()
        assert finalize_deploy.await_args.kwargs == {
            "broadcast_host_message": deps.broadcast_host_message,
            "chat_jid": "slack:C123",
            "commit_sha": "new-sha",
            "config_hash": "config-hash",
            "previous_sha": "old-sha",
            "change_kind": DeployChangeKind.CODE,
            "data_dir": deps.agent_execution_runtime.data_dir,
            "resume_prompt": "Deploy complete. Verifying service health.",
            "sigterm_delay": 0.25,
        }

    @staticmethod
    def _assert_scheduler_status(
        temporal_scheduler,
        *,
        workflow_id: str,
        task_id: str,
        result: str,
    ) -> None:
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == workflow_id
        assert status["last_task_id"] == task_id
        assert status["last_result"] == result

    def test_poller_schedules_bound_custom_catchup_to_their_intervals(self):
        runtime = _scheduler_runtime(
            SchedulerConfig(
                git_sync_interval_seconds=120,
                channel_reconciliation_interval_seconds=180,
            ),
            external_repo_sync_slugs=("owner/project",),
        )
        desired = temporal_schedules.desired_recurring_schedules([], [], runtime)
        schedules = (
            (desired["pynchy-git-sync-host"], timedelta(seconds=120)),
            (
                desired["pynchy-git-sync-repo-owner-project"],
                timedelta(seconds=120),
            ),
            (
                desired["pynchy-channel-reconciliation"],
                timedelta(seconds=180),
            ),
        )

        for schedule, interval in schedules:
            assert schedule.spec.intervals[0].every == interval
            assert schedule.policy.catchup_window == interval

    @pytest.mark.asyncio
    async def test_reconcile_uses_composed_external_repo_sync_slugs(self, monkeypatch):
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime()
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))

        await runtime.reconcile_schedules()

        schedule_ids = {schedule_id for schedule_id, _, _ in client.created_schedules}
        assert "pynchy-git-sync-repo-owner-project" not in schedule_ids

    @pytest.mark.asyncio
    async def test_reconcile_deletes_stale_schedule(self, monkeypatch):

        client = FakeScheduleClient()
        stale_schedule_id = "pynchy-agent-schedule-stale"
        client.schedule_ids = [stale_schedule_id]
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )
        runtime.client = client
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), jobs={})
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))

        await runtime.reconcile_schedules()

        assert client.handles[stale_schedule_id].deleted is True

    @pytest.mark.asyncio
    async def test_worker_lifecycle_updates_running_status(self, monkeypatch):

        def fake_connect(*args, **kwargs):
            return asyncio.sleep(0, result=FakeScheduleClient())

        @asynccontextmanager
        async def fake_worker(*args, **kwargs):
            yield object()

        temporal_scheduler.reset_temporal_scheduler_status()
        monkeypatch.setattr(temporal_scheduler.Client, "connect", fake_connect)
        monkeypatch.setattr(temporal_scheduler, "Worker", fake_worker)
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )

        async with runtime:
            assert temporal_scheduler.get_temporal_scheduler_status()["worker_running"] is True

        assert temporal_scheduler.get_temporal_scheduler_status()["worker_running"] is False

    @pytest.mark.asyncio
    async def test_worker_registers_temporal_orchestration_workflows(self, monkeypatch):

        captured = {}

        def fake_connect(*args, **kwargs):
            return asyncio.sleep(0, result=FakeScheduleClient())

        monkeypatch.setattr(temporal_scheduler.Client, "connect", fake_connect)
        monkeypatch.setattr(temporal_scheduler, "Worker", self._capturing_worker(captured))
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )

        async with runtime:
            self._assert_registered_temporal_workflows(captured, temporal_scheduler)

    @pytest.mark.asyncio
    async def test_startup_failure_unbinds_scheduler_deps(self, monkeypatch, temporal_task):

        def fail_connect(*args, **kwargs):
            raise RuntimeError(TEMPORAL_UNAVAILABLE_MESSAGE)

        scheduler = SchedulerConfig(
            temporal_address="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="pynchy-test",
        )
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(scheduler)
        )

        monkeypatch.setattr(temporal_scheduler.Client, "connect", fail_connect)

        with pytest.raises(RuntimeError, match="temporal unavailable"):
            async with runtime:
                pass

        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["worker_running"] is False
        assert status["last_error"] == "temporal unavailable"

        monkeypatch.setattr(
            temporal_scheduler,
            "get_task_by_id",
            lambda task_id: asyncio.sleep(
                0,
                result=temporal_task if task_id == temporal_task.id else None,
            ),
        )
        with pytest.raises(RuntimeError, match="dependencies are not bound"):
            await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_uses_shared_runner(
        self, monkeypatch, temporal_task
    ):

        deps = NullSchedulerDeps()
        called = {}

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task if task_id == temporal_task.id else None)

        def fake_run_scheduled_agent(task, runner_deps, *, occurrence_id):
            called["task"] = task
            called["deps"] = runner_deps
            called["occurrence_id"] = occurrence_id
            return asyncio.sleep(0, result=TurnOutcome.COMPLETED)

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(
            temporal_scheduler,
            "ensure_scheduled_task_binding",
            AsyncMock(return_value=temporal_task),
        )
        monkeypatch.setattr(temporal_scheduler, "run_scheduled_agent", fake_run_scheduled_agent)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(
                workflow_id="workflow-completed",
                workflow_run_id="occurrence-completed",
            ),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        assert result == "completed"
        assert called == {
            "task": temporal_task,
            "deps": deps,
            "occurrence_id": "occurrence-completed",
        }
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "workflow-completed"
        assert status["last_task_id"] == temporal_task.id
        assert status["last_result"] == "completed"
        assert status["last_completed_at"] is not None

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_returns_terminal_pause(
        self, monkeypatch, temporal_task
    ):
        monkeypatch.setattr(
            temporal_scheduler,
            "get_task_by_id",
            lambda _task_id: asyncio.sleep(0, result=temporal_task),
        )
        monkeypatch.setattr(
            temporal_scheduler,
            "ensure_scheduled_task_binding",
            AsyncMock(return_value=temporal_task),
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(
                workflow_id="workflow-paused",
                workflow_run_id="occurrence-paused",
            ),
        )
        monkeypatch.setattr(
            temporal_scheduler,
            "run_scheduled_agent",
            lambda _task, _deps, *, occurrence_id: asyncio.sleep(
                0,
                result=TurnOutcome.PAUSED,
            ),
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        result = await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        assert result == TurnOutcome.PAUSED.value

    @pytest.mark.asyncio
    async def test_run_learning_review_activity_uses_bound_deps(self, monkeypatch, learning_packet):

        deps = NullSchedulerDeps()
        deps.run_learning_review = AsyncMock(return_value="completed")
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="learning-workflow-completed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_scheduler.run_learning_review(packet_to_payload(learning_packet))

        assert result == "completed"
        deps.run_learning_review.assert_awaited_once_with(learning_packet)
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "learning-workflow-completed"
        assert status["last_task_id"] == learning_packet.job_id
        assert status["last_result"] == "completed"

    @pytest.mark.asyncio
    async def test_run_learning_review_activity_records_and_reraises_failure(self, learning_packet):
        deps = NullSchedulerDeps()
        deps.run_learning_review = AsyncMock(side_effect=RuntimeError("review failed"))
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        with pytest.raises(RuntimeError, match="review failed"):
            await temporal_scheduler.run_learning_review(packet_to_payload(learning_packet))

        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_task_id"] == learning_packet.job_id
        assert status["last_result"] == "error"

    @pytest.mark.asyncio
    async def test_run_interactive_message_activity_uses_bound_deps(self, monkeypatch):

        deps = NullSchedulerDeps()
        called = {}

        def fake_process_message_turn(runner_deps, chat_jid):
            called["deps"] = runner_deps
            called["chat_jid"] = chat_jid
            return asyncio.sleep(0, result=TurnOutcome.COMPLETED)

        monkeypatch.setattr(
            temporal_interactive, "_process_interactive_message_turn", fake_process_message_turn
        )
        monkeypatch.setattr(
            temporal_interactive.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="interactive-workflow-completed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_scheduler.run_interactive_message_turn("slack:C123")

        assert result == "completed"
        assert called == {"deps": deps, "chat_jid": "slack:C123"}
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "interactive-workflow-completed"
        assert status["last_task_id"] == "slack:C123"
        assert status["last_result"] == "completed"

    @pytest.mark.asyncio
    async def test_interactive_activity_waits_for_recovered_workspace_route(self, monkeypatch):
        readiness = StartupReadiness()
        deps = NullSchedulerDeps(startup_readiness=readiness)
        run_turn = AsyncMock(return_value=TurnOutcome.COMPLETED)
        deps.queue.run_message_turn = run_turn  # type: ignore[method-assign]
        temporal_scheduler.bind_scheduler_deps(deps)
        task = asyncio.create_task(temporal_scheduler.run_interactive_message_turn("slack:C123"))

        await asyncio.sleep(0)
        assert not task.done()
        run_turn.assert_not_awaited()

        workspace = WorkspaceProfile(
            jid="slack:C123",
            name="Recovered",
            folder="recovered",
            trigger="@pynchy",
        )
        deps.groups[workspace.jid] = workspace
        readiness.mark_ready()

        assert await task == TurnOutcome.COMPLETED.value
        target = run_turn.await_args.args[0]
        assert target.folder == workspace.folder
        assert target.chat_jid == workspace.jid

    @pytest.mark.asyncio
    async def test_missing_workspace_cannot_complete_before_startup_recovery(self, monkeypatch):
        readiness = StartupReadiness()
        deps = NullSchedulerDeps(startup_readiness=readiness)
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)
        monkeypatch.setattr(
            temporal_interactive.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="missing-before-recovery"),
        )
        task = asyncio.create_task(temporal_scheduler.run_interactive_message_turn("slack:MISSING"))

        await asyncio.sleep(0)
        assert not task.done()
        assert temporal_scheduler.get_temporal_scheduler_status()["last_result"] is None

        readiness.mark_ready()

        assert await task == TurnOutcome.COMPLETED.value
        assert temporal_scheduler.get_temporal_scheduler_status()["last_result"] == "completed"

    @pytest.mark.asyncio
    async def test_interactive_activity_receives_startup_failure(self, monkeypatch):
        readiness = StartupReadiness()
        deps = NullSchedulerDeps(startup_readiness=readiness)
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)
        monkeypatch.setattr(
            temporal_interactive.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="startup-failed"),
        )
        task = asyncio.create_task(temporal_scheduler.run_interactive_message_turn("slack:C123"))

        readiness.mark_failed(RuntimeError("route recovery failed"))

        with pytest.raises(StartupReadinessError, match="Startup route recovery failed") as error:
            await task

        assert isinstance(error.value.__cause__, RuntimeError)
        assert temporal_scheduler.get_temporal_scheduler_status()["last_result"] == "error"

    @pytest.mark.asyncio
    async def test_run_interactive_message_activity_retries_unhandled_turn(self, monkeypatch):

        def fake_process_message_turn(_runner_deps, _chat_jid):
            return asyncio.sleep(0, result=TurnOutcome.RETRY)

        monkeypatch.setattr(
            temporal_interactive, "_process_interactive_message_turn", fake_process_message_turn
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(RuntimeError, match="Interactive message turn requested retry"):
            await temporal_scheduler.run_interactive_message_turn("slack:C123")

    @pytest.mark.asyncio
    async def test_run_interactive_message_activity_continues_after_safe_interrupt(
        self, monkeypatch
    ):
        def fake_process_message_turn(_runner_deps, _chat_jid):
            return asyncio.sleep(0, result=TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT)

        monkeypatch.setattr(
            temporal_interactive, "_process_interactive_message_turn", fake_process_message_turn
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        result = await temporal_scheduler.run_interactive_message_turn("slack:C123")

        assert result == TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT.value

    @pytest.mark.asyncio
    async def test_runtime_continuation_resolves_replacement_chat_binding(self):
        current = WorkspaceProfile(
            jid="slack:CURRENT",
            name="Current",
            folder="admin",
            trigger="@pynchy",
        )
        deps = NullSchedulerDeps(groups={current.jid: current})
        run_turn = AsyncMock(return_value=TurnOutcome.COMPLETED)
        deps.queue.run_message_turn = run_turn  # type: ignore[method-assign]
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_scheduler.run_interactive_runtime_turn("admin")

        assert result == TurnOutcome.COMPLETED.value
        target = run_turn.await_args.args[0]
        assert target.folder == "admin"
        assert target.chat_jid == current.jid

    @pytest.mark.asyncio
    async def test_runtime_continuation_records_missing_workspace_failure(self):
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(RuntimeError, match="Interactive runtime no longer exists"):
            await temporal_scheduler.run_interactive_runtime_turn("retired")

        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_task_id"] == "retired"
        assert status["last_result"] == "error"

    @pytest.mark.asyncio
    async def test_run_interactive_message_activity_returns_terminal_pause(self, monkeypatch):
        monkeypatch.setattr(
            temporal_interactive,
            "_process_interactive_message_turn",
            lambda _deps, _chat_jid: asyncio.sleep(0, result=TurnOutcome.PAUSED),
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        result = await temporal_scheduler.run_interactive_message_turn("slack:C123")

        assert result == TurnOutcome.PAUSED.value

    @pytest.mark.asyncio
    async def test_interrupted_turn_activity_preserves_safe_interrupt_continuation(
        self, monkeypatch
    ):
        get_turn = AsyncMock(
            return_value=InFlightTurn(
                turn_id="turn-123",
                chat_jid="slack:C123",
                group_folder="admin",
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-22T03:43:18+00:00",
            )
        )
        claim_turn = AsyncMock(return_value=True)
        scheduler_deps = NullSchedulerDeps()

        def get_scheduler_deps() -> NullSchedulerDeps:
            return scheduler_deps

        monkeypatch.setattr(temporal_interrupted, "get_in_flight_turn", get_turn)
        monkeypatch.setattr(temporal_interrupted, "claim_in_flight_turn", claim_turn)
        monkeypatch.setattr(
            temporal_interrupted,
            "_dispatch_interrupted_turn",
            AsyncMock(return_value=TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT),
        )
        monkeypatch.setattr(temporal_interrupted, "_require_scheduler_deps", get_scheduler_deps)

        result = await temporal_interrupted.run_interrupted_agent_turn("turn-123")

        assert result == TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT.value

    @pytest.mark.asyncio
    async def test_interrupted_turn_waits_for_startup_before_claiming_or_running(
        self,
        monkeypatch,
    ) -> None:
        await init_test_database()
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-waits-for-routes",
                chat_jid="matrix:room:startup",
                group_folder="matrix-runtime",
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"sender_name": "User", "content": "finish the job"}],
                input_start_cursor="old",
                input_end_cursor="new",
                started_at="2026-07-22T03:43:18+00:00",
            )
        )
        readiness = StartupReadiness()
        deps = NullSchedulerDeps(startup_readiness=readiness)
        dispatch = AsyncMock(return_value=TurnOutcome.COMPLETED)
        heartbeat_active = asyncio.Event()

        @asynccontextmanager
        async def recording_heartbeats(_details):
            heartbeat_active.set()
            yield

        monkeypatch.setattr(temporal_interrupted, "_dispatch_interrupted_turn", dispatch)
        monkeypatch.setattr(temporal_interrupted, "activity_heartbeats", recording_heartbeats)
        temporal_scheduler.bind_scheduler_deps(deps)

        activity_task = asyncio.create_task(
            temporal_interrupted.run_interrupted_agent_turn("turn-waits-for-routes")
        )
        await heartbeat_active.wait()

        checkpoint = await get_in_flight_turn("turn-waits-for-routes")
        assert checkpoint is not None
        assert checkpoint.claimed_at is None
        dispatch.assert_not_awaited()

        readiness.mark_ready()

        assert await activity_task == TurnOutcome.COMPLETED.value
        dispatch.assert_awaited_once_with("turn-waits-for-routes", deps)
