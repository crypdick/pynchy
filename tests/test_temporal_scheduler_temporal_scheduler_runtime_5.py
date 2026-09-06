"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
import pynchy.host.orchestrator.temporal.workflows as temporal_workflows
from pynchy.canary_contracts import (
    CanaryOutcome,
    CanaryRun,
)
from pynchy.config.api import (
    CanaryConfig,
)
from pynchy.deployments import (
    DeployChangeKind,
)
from pynchy.host.orchestrator.scheduled_binding import ScheduledTaskOwnershipError
from pynchy.host.orchestrator.temporal.runtime_state import TemporalActivityInfo
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.state import create_task, get_task_run_logs, init_test_database
from pynchy.turn_outcomes import TurnOutcome
from pynchy.workspace.api import WorkspaceProfile
from tests.temporal_scheduler_support import (
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
            temporal_scheduler.record_terminal_scheduled_task_failure_activity,
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
    async def test_run_scheduled_agent_activity_retries_failed_runner(
        self, monkeypatch, temporal_task
    ):

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task)

        def fake_run_scheduled_agent(task, runner_deps, *, occurrence_id):
            assert occurrence_id == "occurrence-failed"
            return asyncio.sleep(0, result=TurnOutcome.RETRY)

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
                workflow_id="workflow-failed",
                workflow_run_id="occurrence-failed",
            ),
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(RuntimeError, match="Scheduled agent task requested retry"):
            await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_rejects_missing_durable_owner(
        self, monkeypatch, temporal_task
    ):
        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task)

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(
            temporal_scheduler,
            "ensure_scheduled_task_binding",
            AsyncMock(
                side_effect=ScheduledTaskOwnershipError(
                    "Scheduled task owner workspace is unavailable"
                )
            ),
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: TemporalActivityInfo(workflow_id="workflow-unowned"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(ScheduledTaskOwnershipError, match="owner workspace is unavailable"):
            await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "workflow-unowned"
        assert status["last_result"] == "error"

    @pytest.mark.asyncio
    async def test_scheduled_agent_workflow_runs_one_owned_activity(self, monkeypatch):
        execute_activity = AsyncMock(return_value="completed")
        monkeypatch.setattr(temporal_workflows.workflow, "execute_activity", execute_activity)

        result = await temporal_workflows.ScheduledAgentTaskWorkflow().run("task-1")

        assert result == "completed"
        execute_activity.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scheduled_agent_workflow_clears_terminal_checkpoint(self, monkeypatch):
        terminal_error = ActivityError(
            "scheduled task failed",
            scheduled_event_id=1,
            started_event_id=2,
            identity="test-worker",
            activity_type="run_scheduled_agent_task",
            activity_id="activity-1",
            retry_state=None,
        )
        terminal_error.__cause__ = ApplicationError("WorkerShutdown", type="worker_shutdown")
        execute_activity = AsyncMock(side_effect=[terminal_error, "recorded", "cleared"])
        monkeypatch.setattr(temporal_workflows.workflow, "execute_activity", execute_activity)
        workflow_info = type("WorkflowInfo", (), {"workflow_id": "workflow-1", "run_id": "run-1"})()

        def fake_workflow_info():
            return workflow_info

        monkeypatch.setattr(
            temporal_workflows.workflow,
            "info",
            fake_workflow_info,
        )

        with pytest.raises(ActivityError, match="scheduled task failed"):
            await temporal_workflows.ScheduledAgentTaskWorkflow().run("task-1")

        assert execute_activity.await_count == 3
        assert execute_activity.await_args_list[1].args == (
            "record_terminal_scheduled_task_failure",
            {
                "task_id": "task-1",
                "workflow_id": "workflow-1",
                "workflow_run_id": "run-1",
                "error": "ApplicationError: worker_shutdown: WorkerShutdown",
            },
        )
        assert execute_activity.await_args_list[2].args == (
            "clear_terminal_scheduled_turn",
            "task-1",
        )

    @pytest.mark.asyncio
    async def test_scheduled_agent_workflow_preserves_checkpoint_when_recording_fails(
        self, monkeypatch
    ):
        terminal_error = ActivityError(
            "scheduled task failed",
            scheduled_event_id=1,
            started_event_id=2,
            identity="test-worker",
            activity_type="run_scheduled_agent_task",
            activity_id="activity-1",
            retry_state=None,
        )
        execute_activity = AsyncMock(side_effect=[terminal_error, RuntimeError("recording failed")])
        monkeypatch.setattr(temporal_workflows.workflow, "execute_activity", execute_activity)

        def fake_workflow_info():
            return type("WorkflowInfo", (), {"workflow_id": "workflow-1", "run_id": "run-1"})()

        monkeypatch.setattr(temporal_workflows.workflow, "info", fake_workflow_info)

        with pytest.raises(RuntimeError, match="recording failed"):
            await temporal_workflows.ScheduledAgentTaskWorkflow().run("task-1")

        assert execute_activity.await_count == 2
        assert execute_activity.await_args_list[1].args[0] == (
            "record_terminal_scheduled_task_failure"
        )

    @pytest.mark.asyncio
    async def test_retry_exhaustion_records_one_terminal_failure(self):
        await init_test_database()
        task = ScheduledTask(
            id="terminal-failure-task",
            group_folder="group",
            chat_jid="slack:group",
            prompt="run",
            schedule_type="once",
            schedule_value="2026-09-06T20:00:00+00:00",
            session_policy=SessionPolicy.CONTINUE,
        )
        await create_task(task)
        attempts = 0
        terminal_payload: dict[str, str] = {}
        recording_activity_identity: dict[str, str] = {}
        task_queue = f"pynchy-temporal-test-{uuid4()}"

        @activity.defn(name="run_scheduled_agent_task")
        async def fail_scheduled_task(_task_id: str) -> str:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0)
            raise ApplicationError("WorkerShutdown", type="worker_shutdown")

        @activity.defn(name="record_terminal_scheduled_task_failure")
        async def record_terminal_failure(payload: dict[str, str]) -> str:
            terminal_payload.update(payload)
            info = activity.info()
            recording_activity_identity.update(
                workflow_id=info.workflow_id,
                workflow_run_id=info.workflow_run_id,
            )
            return await temporal_scheduler.record_terminal_scheduled_task_failure_activity(payload)

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[temporal_workflows.ScheduledAgentTaskWorkflow],
                activities=[
                    fail_scheduled_task,
                    record_terminal_failure,
                    temporal_scheduler.clear_terminal_scheduled_turn,
                ],
                workflow_runner=temporal_scheduler.scheduler_workflow_runner(),
            ):
                with pytest.raises(WorkflowFailureError):
                    await env.client.execute_workflow(
                        temporal_workflows.ScheduledAgentTaskWorkflow.run,
                        task.id,
                        id=f"pynchy-temporal-test-{uuid4()}",
                        task_queue=task_queue,
                    )
        finally:
            await env.shutdown()

        logs = await get_task_run_logs(task.id)
        assert attempts == 3
        assert len(logs) == 1
        assert logs[0].status == "error"
        assert logs[0].error == "ApplicationError: worker_shutdown: WorkerShutdown"
        assert logs[0].temporal_workflow_id == terminal_payload["workflow_id"]
        assert logs[0].temporal_workflow_id == recording_activity_identity["workflow_id"]
        assert logs[0].temporal_workflow_run_id == terminal_payload["workflow_run_id"]
        assert logs[0].temporal_workflow_run_id == recording_activity_identity["workflow_run_id"]
        assert logs[0].escalation_reason == "temporal_retry_exhausted"

    @pytest.mark.asyncio
    async def test_run_scheduled_canaries_uses_configured_target(self, monkeypatch):
        deps = NullSchedulerDeps()
        deps.groups = {
            "admin": WorkspaceProfile(
                jid="slack:admin",
                name="admin",
                folder="admin",
                trigger="always",
                is_admin=True,
            ),
        }
        deps.broadcast_host_message = AsyncMock()
        deps.scheduler_runtime = _scheduler_runtime(
            canary=CanaryConfig(
                enabled=True,
                target_profile="external-canary",
                scenario_ids=["calendar.round.trip"],
            )
        )
        runner = AsyncMock(
            return_value=[
                CanaryRun(
                    run_id="run-1",
                    scenario_id="calendar.round.trip",
                    action_ids=("calendar.event.create",),
                    target_profile="external-canary",
                    code_revision="code",
                    config_revision="config",
                    started_at="2026-07-16T00:00:00+00:00",
                    completed_at="2026-07-16T00:01:00+00:00",
                    outcome=CanaryOutcome.PASSED,
                    error_class="ProviderUnavailable",
                    is_regression=True,
                    starts_regression=True,
                )
            ]
        )
        deps.run_declared_canaries = runner
        temporal_scheduler.bind_scheduler_deps(deps)
        temporal_scheduler.reset_temporal_scheduler_status()

        result = await temporal_scheduler.run_scheduled_canaries()

        assert result == "completed:1"
        runner.assert_awaited_once_with("external-canary", ("calendar.round.trip",))
        deps.broadcast_host_message.assert_awaited_once_with(
            "slack:admin",
            "Canary regression: calendar.round.trip on external-canary "
            "(ProviderUnavailable). See /canaries/report for the unresolved regression report.",
        )
        assert temporal_scheduler.get_temporal_scheduler_status()["last_result"] == "completed"

    @pytest.mark.asyncio
    async def test_run_scheduled_canaries_notifies_admins_of_recovery(self):
        deps = NullSchedulerDeps()
        deps.groups = {
            "admin": WorkspaceProfile(
                jid="slack:admin",
                name="admin",
                folder="admin",
                trigger="always",
                is_admin=True,
            ),
        }
        deps.broadcast_host_message = AsyncMock()
        deps.scheduler_runtime = _scheduler_runtime(
            canary=CanaryConfig(
                enabled=True,
                target_profile="external-canary",
                scenario_ids=["calendar.round.trip"],
            )
        )
        deps.run_declared_canaries = AsyncMock(
            return_value=[
                CanaryRun(
                    run_id="run-1",
                    scenario_id="calendar.round.trip",
                    action_ids=("calendar.event.create",),
                    target_profile="external-canary",
                    code_revision="code",
                    config_revision="config",
                    started_at="2026-07-16T00:00:00+00:00",
                    completed_at="2026-07-16T00:01:00+00:00",
                    outcome=CanaryOutcome.PASSED,
                    is_recovery=True,
                )
            ]
        )
        temporal_scheduler.bind_scheduler_deps(deps)
        try:
            result = await temporal_scheduler.run_scheduled_canaries()
        finally:
            temporal_scheduler.bind_scheduler_deps(None)

        assert result == "completed:1"
        deps.broadcast_host_message.assert_awaited_once_with(
            "slack:admin",
            "Canary recovered: calendar.round.trip on external-canary. "
            "See /canaries/report for current evidence.",
        )

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_workflow_executes_activity_through_temporal_worker(
        self, monkeypatch, temporal_task
    ):
        """Temporal can run the Pynchy workflow and activity path end to end."""
        deps = NullSchedulerDeps()
        called = {}
        task_queue = f"pynchy-temporal-test-{uuid4()}"

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task if task_id == temporal_task.id else None)

        def fake_run_scheduled_agent(task, runner_deps, **_kwargs):
            called["task_id"] = task.id
            called["deps"] = runner_deps
            return asyncio.sleep(0, result=TurnOutcome.COMPLETED)

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(temporal_scheduler, "run_scheduled_agent", fake_run_scheduled_agent)
        monkeypatch.setattr(
            temporal_scheduler,
            "ensure_scheduled_task_binding",
            AsyncMock(return_value=temporal_task),
        )
        temporal_scheduler.bind_scheduler_deps(deps)

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[temporal_workflows.ScheduledAgentTaskWorkflow],
                activities=[temporal_scheduler.run_scheduled_agent_task],
                workflow_runner=temporal_scheduler.scheduler_workflow_runner(),
            ):
                result = await env.client.execute_workflow(
                    temporal_workflows.ScheduledAgentTaskWorkflow.run,
                    temporal_task.id,
                    id=f"pynchy-temporal-test-{uuid4()}",
                    task_queue=task_queue,
                )
        finally:
            temporal_scheduler.bind_scheduler_deps(None)
            await env.shutdown()

        assert result == "completed"
        assert called == {"task_id": temporal_task.id, "deps": deps}

    @pytest.mark.asyncio
    async def test_database_host_job_failure_does_not_retry_side_effects(self):
        """A recurring host job may retry only at its next schedule instant."""
        attempts = 0
        task_queue = f"pynchy-temporal-test-{uuid4()}"

        @activity.defn(name="run_database_host_job")
        async def fail_host_job(_job_id: str) -> str:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0)
            raise RuntimeError("expected command failure")

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[temporal_workflows.DatabaseHostJobWorkflow],
                activities=[fail_host_job],
                workflow_runner=temporal_scheduler.scheduler_workflow_runner(),
            ):
                with pytest.raises(WorkflowFailureError):
                    await env.client.execute_workflow(
                        temporal_workflows.DatabaseHostJobWorkflow.run,
                        "job-id",
                        id=f"pynchy-temporal-test-{uuid4()}",
                        task_queue=task_queue,
                    )
        finally:
            await env.shutdown()

        assert attempts == 1

    @pytest.mark.asyncio
    async def test_config_host_job_failure_does_not_retry_side_effects(self):
        """A config host command may retry only at its next schedule instant."""
        attempts = 0
        task_queue = f"pynchy-temporal-test-{uuid4()}"

        @activity.defn(name="run_config_host_cron_job")
        async def fail_host_job(_job_name: str) -> str:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0)
            raise RuntimeError("expected command failure")

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[temporal_workflows.ConfigHostCronWorkflow],
                activities=[fail_host_job],
                workflow_runner=temporal_scheduler.scheduler_workflow_runner(),
            ):
                with pytest.raises(WorkflowFailureError):
                    await env.client.execute_workflow(
                        temporal_workflows.ConfigHostCronWorkflow.run,
                        "backup-db",
                        id=f"pynchy-temporal-test-{uuid4()}",
                        task_queue=task_queue,
                    )
        finally:
            await env.shutdown()

        assert attempts == 1
