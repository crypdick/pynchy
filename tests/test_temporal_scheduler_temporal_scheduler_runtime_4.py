"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import get_type_hints
from unittest.mock import AsyncMock

import pytest
from temporalio.converter import DataConverter

import pynchy.host.orchestrator.temporal.channel_reconciliation as temporal_channel_reconciliation
import pynchy.host.orchestrator.temporal.deploy as temporal_deploy
import pynchy.host.orchestrator.temporal.interrupted as temporal_interrupted
import pynchy.host.orchestrator.temporal.linear_work_items as temporal_linear_work_items
import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
import pynchy.host.orchestrator.temporal.workflows as temporal_workflows
from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.deployments import (
    DeployChangeKind,
)
from pynchy.host.orchestrator.deploy import BuildResult, RollbackResult
from pynchy.host.orchestrator.startup_readiness import (
    StartupReadiness,
    StartupReadinessError,
)
from pynchy.host.orchestrator.temporal.runtime_state import TemporalActivityInfo
from pynchy.state import (
    begin_in_flight_turn,
    claim_in_flight_turn,
    get_in_flight_turn,
    init_test_database,
)
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile
from tests.temporal_scheduler_support import (
    NullSchedulerDeps,
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

    @pytest.mark.asyncio
    async def test_interrupted_turn_startup_failure_leaves_checkpoint_unclaimed(
        self,
        monkeypatch,
    ) -> None:
        await init_test_database()
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-startup-failed",
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
        dispatch = AsyncMock()
        monkeypatch.setattr(temporal_interrupted, "_dispatch_interrupted_turn", dispatch)
        temporal_scheduler.bind_scheduler_deps(deps)
        activity_task = asyncio.create_task(
            temporal_interrupted.run_interrupted_agent_turn("turn-startup-failed")
        )

        readiness.mark_failed(RuntimeError("route recovery failed"))

        with pytest.raises(StartupReadinessError, match="Startup route recovery failed"):
            await activity_task

        checkpoint = await get_in_flight_turn("turn-startup-failed")
        assert checkpoint is not None
        assert checkpoint.claimed_at is None
        dispatch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_interrupted_turn_activity_does_not_claim_paused_checkpoint(self, monkeypatch):
        await init_test_database()
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-paused",
                chat_jid="slack:C123",
                group_folder="admin",
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-22T03:43:18+00:00",
                control_state=CheckpointControlState.PAUSED,
            )
        )
        claim = AsyncMock(wraps=claim_in_flight_turn)
        monkeypatch.setattr(temporal_interrupted, "claim_in_flight_turn", claim)
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        result = await temporal_interrupted.run_interrupted_agent_turn("turn-paused")

        assert result == TurnOutcome.PAUSED.value
        claim.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancelled_interrupted_turn_activity_releases_durable_claim(self, monkeypatch):
        await init_test_database()
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-cancelled",
                chat_jid="slack:C123",
                group_folder="admin",
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"sender_name": "User", "content": "finish the job"}],
                input_start_cursor="old",
                input_end_cursor="new",
                started_at="2026-07-22T03:43:18+00:00",
                interrupted_at="2026-07-22T03:51:57+00:00",
                deploy_id="deploy-sha",
            )
        )
        monkeypatch.setattr(
            temporal_interrupted,
            "_dispatch_interrupted_turn",
            AsyncMock(side_effect=asyncio.CancelledError),
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(asyncio.CancelledError):
            await temporal_interrupted.run_interrupted_agent_turn("turn-cancelled")

        checkpoint = await get_in_flight_turn("turn-cancelled")
        assert checkpoint is not None
        assert checkpoint.claimed_at is None
        assert await claim_in_flight_turn("turn-cancelled") is True
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_task_id"] == "turn-cancelled"
        assert status["last_result"] == "cancelled"

    @pytest.mark.asyncio
    async def test_failed_interrupted_turn_activity_releases_durable_claim(self, monkeypatch):
        await init_test_database()
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-failed",
                chat_jid="slack:C123",
                group_folder="admin",
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"sender_name": "User", "content": "finish the job"}],
                input_start_cursor="old",
                input_end_cursor="new",
                started_at="2026-07-22T03:43:18+00:00",
            )
        )
        monkeypatch.setattr(
            temporal_interrupted,
            "_dispatch_interrupted_turn",
            AsyncMock(side_effect=RuntimeError("resume failed")),
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(RuntimeError, match="resume failed"):
            await temporal_interrupted.run_interrupted_agent_turn("turn-failed")

        checkpoint = await get_in_flight_turn("turn-failed")
        assert checkpoint is not None
        assert checkpoint.claimed_at is None

    @pytest.mark.asyncio
    async def test_run_deploy_activity_builds_then_finalizes(self, monkeypatch):
        await init_test_database()
        deps = NullSchedulerDeps()
        deps.broadcast_host_message = AsyncMock()
        finalize_deploy = AsyncMock()
        request = temporal_deploy.DeployRequest(
            chat_jid="slack:C123",
            commit_sha="new-sha",
            config_hash="config-hash",
            previous_sha="old-sha",
            change_kind=DeployChangeKind.CODE,
            rebuild=True,
            reason="test",
        )

        monkeypatch.setattr(
            temporal_deploy,
            "build_container_image",
            lambda _project_root: BuildResult(success=True),
        )
        monkeypatch.setattr(temporal_deploy, "finalize_deploy", finalize_deploy)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="deploy-workflow-completed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_deploy.run_deploy(
            temporal_deploy.deploy_request_to_payload(request)
        )

        assert result == "restart_requested"
        self._assert_finalize_deploy_kwargs(finalize_deploy, deps)
        self._assert_scheduler_status(
            temporal_scheduler,
            workflow_id="deploy-workflow-completed",
            task_id="new-sha",
            result="restart_requested",
        )

    @pytest.mark.asyncio
    async def test_run_deploy_activity_reports_build_failure(self, monkeypatch):
        await init_test_database()
        deps = NullSchedulerDeps()
        deps.broadcast_host_message = AsyncMock()
        finalize_deploy = AsyncMock()
        request = temporal_deploy.DeployRequest(
            chat_jid="slack:C123",
            commit_sha="new-sha",
            config_hash="config-hash",
            previous_sha="old-sha",
            rebuild=True,
        )

        monkeypatch.setattr(
            temporal_deploy,
            "build_container_image",
            lambda _project_root: BuildResult(success=False, stderr="image build exploded"),
        )
        rollback_calls: list[str] = []

        def rollback_deploy_checkout(sha: str) -> RollbackResult:
            rollback_calls.append(sha)
            return RollbackResult(success=True, actual_sha="old-sha-full")

        monkeypatch.setattr(
            temporal_deploy,
            "rollback_deploy_checkout",
            rollback_deploy_checkout,
            raising=False,
        )
        monkeypatch.setattr(temporal_deploy, "finalize_deploy", finalize_deploy)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="deploy-workflow-failed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_deploy.run_deploy(
            temporal_deploy.deploy_request_to_payload(request)
        )

        assert result == "build_failed_rolled_back"
        finalize_deploy.assert_not_awaited()
        assert rollback_calls == ["old-sha"]
        deps.broadcast_host_message.assert_awaited_once_with(
            "slack:C123",
            "Auto-deploy new-sha failed: Container rebuild failed: image build exploded\n"
            "Rolled back to old-sha-full.\n"
            "Server health: healthy (current service was not restarted).",
        )
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "deploy-workflow-failed"
        assert status["last_task_id"] == "new-sha"
        assert status["last_result"] == "build_failed_rolled_back"
        assert status["last_error"] == "Container rebuild failed: image build exploded"

    @pytest.mark.asyncio
    async def test_run_deploy_activity_reports_restart_preparation_failure(self, monkeypatch):
        await init_test_database()
        deps = NullSchedulerDeps()
        deps.broadcast_host_message = AsyncMock()
        request = temporal_deploy.DeployRequest(
            chat_jid="slack:C123",
            commit_sha="new-sha",
            config_hash="config-hash",
            previous_sha="old-sha",
            rebuild=False,
        )
        finalize_deploy = AsyncMock(side_effect=RuntimeError("database unavailable"))
        rollback_calls: list[str] = []

        def rollback_deploy_checkout(sha: str) -> RollbackResult:
            rollback_calls.append(sha)
            return RollbackResult(success=True, actual_sha="old-sha-full")

        monkeypatch.setattr(temporal_deploy, "finalize_deploy", finalize_deploy)
        monkeypatch.setattr(
            temporal_deploy,
            "rollback_deploy_checkout",
            rollback_deploy_checkout,
            raising=False,
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="deploy-workflow-restart-failed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_deploy.run_deploy(
            temporal_deploy.deploy_request_to_payload(request)
        )

        assert result == "restart_failed_rolled_back"
        assert rollback_calls == ["old-sha"]
        deps.broadcast_host_message.assert_awaited_once_with(
            "slack:C123",
            "Auto-deploy new-sha failed: Restart preparation failed: "
            "RuntimeError: database unavailable\n"
            "Rolled back to old-sha-full.\n"
            "Server health: healthy (current service was not restarted).",
        )

    @pytest.mark.asyncio
    async def test_run_channel_reconciliation_activity_uses_bound_deps(self, monkeypatch):
        deps = NullSchedulerDeps()
        called = {}

        def fake_reconcile_all_channels(runner_deps):
            called["deps"] = runner_deps
            return asyncio.sleep(0, result=None)

        monkeypatch.setattr(
            temporal_channel_reconciliation,
            "reconcile_all_channels",
            fake_reconcile_all_channels,
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="channel-reconcile-completed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_channel_reconciliation.run_channel_reconciliation()

        assert result == "completed"
        assert called == {"deps": deps}
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "channel-reconcile-completed"
        assert status["last_task_id"] == "channel-reconciliation"
        assert status["last_result"] == "completed"

    @pytest.mark.asyncio
    async def test_run_channel_reconciliation_activity_records_failure(self, monkeypatch):
        deps = NullSchedulerDeps()
        fail_reconciliation = AsyncMock(
            side_effect=RuntimeError("slack/group: history unavailable")
        )

        monkeypatch.setattr(
            temporal_channel_reconciliation,
            "reconcile_all_channels",
            fail_reconciliation,
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="channel-reconcile-failed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        with pytest.raises(RuntimeError, match="slack/group: history unavailable"):
            await temporal_channel_reconciliation.run_channel_reconciliation()

        fail_reconciliation.assert_awaited_once_with(deps)
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "channel-reconcile-failed"
        assert status["last_task_id"] == "channel-reconciliation"
        assert status["last_result"] == "error"
        assert status["last_error"] == "slack/group: history unavailable"

    @pytest.mark.asyncio
    async def test_run_linear_work_item_reconciliation_uses_managed_boards(self, monkeypatch):
        deps = NullSchedulerDeps(
            groups={
                "discord:project": WorkspaceProfile(
                    jid="discord:project",
                    name="Project",
                    folder="project",
                    trigger="@Pynchy",
                )
            }
        )
        deps.reconcile_linear_work_items = AsyncMock(return_value=2)
        monkeypatch.setattr(
            temporal_linear_work_items.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="linear-work-items"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_linear_work_items.run_linear_work_item_reconciliation()

        assert result == "completed:2"
        deps.reconcile_linear_work_items.assert_awaited_once_with()
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["tracked_results"]["linear-work-item-reconciliation"]["result"] == (
            "completed:2"
        )

    @pytest.mark.asyncio
    async def test_run_linear_work_item_reconciliation_reports_disabled(self, monkeypatch):
        deps = NullSchedulerDeps()
        deps.reconcile_linear_work_items = AsyncMock(return_value=None)
        monkeypatch.setattr(
            temporal_linear_work_items.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="linear-work-items-disabled"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        assert await temporal_linear_work_items.run_linear_work_item_reconciliation() == "disabled"

    @pytest.mark.asyncio
    async def test_run_linear_work_item_reconciliation_surfaces_failure(self, monkeypatch):
        deps = NullSchedulerDeps()
        deps.reconcile_linear_work_items = AsyncMock(side_effect=RuntimeError("reconcile failed"))
        monkeypatch.setattr(
            temporal_linear_work_items.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="linear-work-items-failed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        with pytest.raises(RuntimeError, match="reconcile failed"):
            await temporal_linear_work_items.run_linear_work_item_reconciliation()

    @pytest.mark.asyncio
    async def test_run_linear_plan_review_admission_surfaces_failure(self, monkeypatch):
        deps = NullSchedulerDeps()
        deps.process_linear_plan_review_admission = AsyncMock(
            side_effect=RuntimeError("review failed")
        )

        @asynccontextmanager
        async def no_heartbeats(_activity_id):
            yield

        monkeypatch.setattr(
            temporal_linear_work_items.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="linear-plan-review-failed"),
        )
        monkeypatch.setattr(temporal_linear_work_items, "activity_heartbeats", no_heartbeats)
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        with pytest.raises(RuntimeError, match="review failed"):
            await temporal_linear_work_items.run_linear_plan_review_admission(
                {
                    "workspace": "project",
                    "issue_id": "issue-1",
                    "identifier": "SYN-1",
                    "updated_at": "2026-07-28T12:00:00Z",
                    "public_source": True,
                }
            )
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["tracked_results"]["linear-plan-review:SYN-1"]["error"] == "review failed"

    @pytest.mark.asyncio
    async def test_run_linear_plan_review_admission_isolated_by_issue(self, monkeypatch):
        deps = NullSchedulerDeps()
        deps.process_linear_plan_review_admission = AsyncMock(return_value=True)

        @asynccontextmanager
        async def no_heartbeats(_activity_id):
            yield

        monkeypatch.setattr(
            temporal_linear_work_items.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="linear-plan-review-syn-1"),
        )
        monkeypatch.setattr(
            temporal_linear_work_items,
            "activity_heartbeats",
            no_heartbeats,
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)
        payload = {
            "workspace": "project",
            "issue_id": "issue-1",
            "identifier": "SYN-1",
            "updated_at": "2026-07-28T12:00:00Z",
            "public_source": True,
        }
        encoded = await DataConverter.default.encode([payload])
        payload_hint = get_type_hints(temporal_linear_work_items.run_linear_plan_review_admission)[
            "payload"
        ]

        [decoded] = await DataConverter.default.decode(encoded, [payload_hint])
        result = await temporal_linear_work_items.run_linear_plan_review_admission(decoded)

        assert result == "admitted"
        deps.process_linear_plan_review_admission.assert_awaited_once()
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["tracked_results"]["linear-plan-review:SYN-1"]["result"] == "admitted"

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_skips_paused_task(self, monkeypatch, temporal_task):

        temporal_task.status = "paused"

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task)

        def fake_run_scheduled_agent(task, runner_deps, **_kwargs):
            raise AssertionError(PAUSED_TASK_RUN_MESSAGE)

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(temporal_scheduler, "run_scheduled_agent", fake_run_scheduled_agent)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="workflow-skipped"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        result = await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        assert result == "skipped"
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "workflow-skipped"
        assert status["last_result"] == "skipped"

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_skips_a_stale_once_workflow(
        self, monkeypatch, temporal_task
    ):
        temporal_task.schedule_type = "once"
        temporal_task.schedule_value = "2026-12-31T23:59:59+00:00"
        run_scheduled_agent = AsyncMock()
        monkeypatch.setattr(
            temporal_scheduler,
            "get_task_by_id",
            AsyncMock(return_value=temporal_task),
        )
        monkeypatch.setattr(
            temporal_scheduler,
            "run_scheduled_agent",
            run_scheduled_agent,
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="pynchy-agent-task-old-definition"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()

        result = await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        assert result == "skipped"
        run_scheduled_agent.assert_not_awaited()
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "pynchy-agent-task-old-definition"
        assert status["last_result"] == "skipped"

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_skips_once_workflow_after_cron_conversion(
        self, monkeypatch, temporal_task
    ):
        run_scheduled_agent = AsyncMock()
        monkeypatch.setattr(
            temporal_scheduler,
            "get_task_by_id",
            AsyncMock(return_value=temporal_task),
        )
        monkeypatch.setattr(
            temporal_scheduler,
            "run_scheduled_agent",
            run_scheduled_agent,
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="pynchy-agent-task-old-once-definition"),
        )

        result = await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        assert temporal_task.schedule_type == "cron"
        assert result == "skipped"
        run_scheduled_agent.assert_not_awaited()
