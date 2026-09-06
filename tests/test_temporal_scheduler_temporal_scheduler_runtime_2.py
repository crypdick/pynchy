"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import make_settings
from temporalio.service import RPCError, RPCStatusCode

import pynchy.host.orchestrator.temporal.host_jobs as temporal_host_jobs
import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
import pynchy.host.orchestrator.temporal.schedules as temporal_schedules
import pynchy.host.orchestrator.temporal.workflows as temporal_workflows
from pynchy.config.api import (
    CanaryConfig,
    JobConfig,
    SchedulerConfig,
)
from pynchy.deployments import (
    DeployChangeKind,
)
from pynchy.host.orchestrator.host_shell import ShellResult
from pynchy.scheduling.api import (
    HostJob,
)
from tests.temporal_scheduler_support import (
    DeduplicatingFakeScheduleClient,
    FakeScheduleClient,
    NullSchedulerDeps,
    _scheduler_runtime,
    _WorkflowListEntry,
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
    async def test_resumed_task_does_not_cancel_a_different_task_with_colliding_workflow_id(
        self, monkeypatch, temporal_task
    ):
        temporal_task.id = "task/a"
        temporal_task.schedule_type = "once"
        temporal_task.schedule_value = "2026-07-25T05:16:14+00:00"
        temporal_task.superseded_occurrence_due_at = temporal_task.schedule_value
        temporal_task.superseded_occurrence_generation = 0
        temporal_task.occurrence_due_at = datetime.now(UTC).isoformat()
        temporal_task.occurrence_generation = 1
        unrelated = replace(
            temporal_task,
            id="task?a",
            schedule_value=temporal_task.schedule_value,
            occurrence_due_at=None,
            occurrence_generation=0,
            superseded_occurrence_due_at=None,
            superseded_occurrence_generation=None,
        )
        unrelated_workflow_id = temporal_schedules.agent_task_workflow_id(unrelated)
        unrelated_run_id = "unrelated-running-occurrence"
        client = DeduplicatingFakeScheduleClient()
        client.workflow_ids.add(unrelated_workflow_id)
        client.workflow_executions = [
            _WorkflowListEntry(
                id=unrelated_workflow_id,
                run_id=unrelated_run_id,
                workflow_type="ScheduledAgentTaskWorkflow",
                execution_time=datetime.now(UTC),
            )
        ]
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=_scheduler_runtime(SchedulerConfig(temporal_task_queue="pynchy-test")),
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler,
            "get_all_tasks",
            AsyncMock(return_value=[temporal_task, unrelated]),
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=runtime.scheduler_config, jobs={})

        await runtime.reconcile_schedules()

        assert (unrelated_workflow_id, unrelated_run_id) not in client.workflow_handles
        assert len(client.started_workflows) == 1
        assert client.started_workflows[0][1] == (temporal_task.id,)
        resumed_workflow_id = client.started_workflows[0][2]["id"]
        assert resumed_workflow_id == temporal_schedules.agent_task_workflow_id(temporal_task)
        assert resumed_workflow_id != unrelated_workflow_id

    @pytest.mark.asyncio
    async def test_reconcile_starts_once_database_host_job_as_delayed_workflow(self, monkeypatch):
        due_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        host_job = HostJob(
            id="host/job one",
            name="backup",
            command="scripts/backup_runtime_dbs.sh",
            schedule_type="once",
            schedule_value=due_at,
            created_by="admin-1",
            status="active",
            enabled=True,
        )
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=_scheduler_runtime(SchedulerConfig(temporal_task_queue="pynchy-test")),
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[host_job])
        )
        settings = make_settings(timezone="UTC", scheduler=runtime.scheduler_config, jobs={})

        await runtime.reconcile_schedules()

        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == temporal_workflows.DatabaseHostJobWorkflow.run
        assert args == (host_job.id,)
        assert kwargs["id"].startswith("pynchy-host-job-host-job-one-")
        assert kwargs["task_queue"] == "pynchy-test"
        assert 0 < kwargs["start_delay"].total_seconds() <= 300

    @pytest.mark.asyncio
    async def test_reconcile_cancels_only_orphaned_future_one_shot_workflows(
        self, monkeypatch, temporal_task
    ):
        due_at = datetime.now(UTC) + timedelta(minutes=5)
        temporal_task.schedule_type = "once"
        temporal_task.schedule_value = due_at.isoformat()
        paused_host_job = HostJob(
            id="paused host job",
            name="backup",
            command="scripts/backup_runtime_dbs.sh",
            schedule_type="once",
            schedule_value=due_at.isoformat(),
            created_by="admin-1",
            status="paused",
            enabled=False,
        )
        desired_task_id = temporal_schedules.agent_task_workflow_id(temporal_task)
        desired_host_id = temporal_schedules.database_host_job_workflow_id(paused_host_job)
        executions = [
            _WorkflowListEntry(
                id=desired_task_id,
                run_id="desired-task-run",
                workflow_type="ScheduledAgentTaskWorkflow",
                execution_time=due_at,
            ),
            _WorkflowListEntry(
                id=desired_host_id,
                run_id="desired-host-run",
                workflow_type="DatabaseHostJobWorkflow",
                execution_time=due_at,
            ),
            _WorkflowListEntry(
                id="pynchy-host-job-deleted-row-old-time",
                run_id="orphan-run",
                workflow_type="DatabaseHostJobWorkflow",
                execution_time=due_at,
            ),
            _WorkflowListEntry(
                id="pynchy-agent-task-already-due-old-time",
                run_id="due-run",
                workflow_type="ScheduledAgentTaskWorkflow",
                execution_time=datetime.now(UTC) - timedelta(seconds=1),
            ),
            _WorkflowListEntry(
                id="pynchy-agent-task-foreign",
                run_id="foreign-run",
                workflow_type="InteractiveMessageWorkflow",
                execution_time=due_at,
            ),
        ]
        client = FakeScheduleClient()
        client.workflow_executions = executions
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(
            temporal_scheduler,
            "get_all_host_jobs",
            AsyncMock(return_value=[paused_host_job]),
        )
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), jobs={})

        await runtime.reconcile_schedules()

        assert client.workflow_query == 'ExecutionStatus = "Running"'
        cancelled_keys = {
            key for key, handle in client.workflow_handles.items() if handle.cancelled
        }
        assert cancelled_keys == {("pynchy-host-job-deleted-row-old-time", "orphan-run")}

    @pytest.mark.asyncio
    async def test_reconcile_cancels_a_previous_timestamp_for_an_existing_once_task(
        self, monkeypatch, temporal_task
    ):
        due_at = datetime.now(UTC) + timedelta(minutes=5)
        temporal_task.schedule_type = "once"
        temporal_task.schedule_value = due_at.isoformat()
        stale_workflow_id = "pynchy-agent-task-task-with-spaces-previous-time"
        client = FakeScheduleClient()
        client.workflow_executions = [
            _WorkflowListEntry(
                id=stale_workflow_id,
                run_id="stale-run",
                workflow_type="ScheduledAgentTaskWorkflow",
                execution_time=due_at,
            )
        ]
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), jobs={})

        await runtime.reconcile_schedules()

        assert client.workflow_handles[stale_workflow_id, "stale-run"].cancelled is True
        assert client.started_workflows[0][2]["id"] == temporal_schedules.agent_task_workflow_id(
            temporal_task
        )

    @pytest.mark.asyncio
    async def test_reconcile_ignores_an_orphan_that_disappears_before_cancel(self, monkeypatch):
        due_at = datetime.now(UTC) + timedelta(minutes=5)
        workflow_id = "pynchy-agent-task-deleted-row-old-time"
        run_id = "missing-run"
        client = FakeScheduleClient()
        client.workflow_executions = [
            _WorkflowListEntry(
                id=workflow_id,
                run_id=run_id,
                workflow_type="ScheduledAgentTaskWorkflow",
                execution_time=due_at,
            )
        ]
        handle = client.get_workflow_handle(workflow_id, run_id=run_id)
        handle.cancel_error = RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), jobs={})

        await runtime.reconcile_schedules()

        assert handle.cancelled is False

    @pytest.mark.asyncio
    async def test_reconcile_creates_temporal_schedule_for_database_host_job(self, monkeypatch):

        host_job = HostJob(
            id="host/job one",
            name="backup",
            command="scripts/backup_runtime_dbs.sh",
            schedule_type="interval",
            schedule_value="60000",
            created_by="admin-1",
            status="active",
            enabled=True,
        )
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(SchedulerConfig())
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[host_job])
        )
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), jobs={})

        await runtime.reconcile_schedules()

        schedules = {schedule_id: schedule for schedule_id, schedule, _ in client.created_schedules}
        schedule = schedules["pynchy-host-job-schedule-host-job-one"]
        schedule_id = "pynchy-host-job-schedule-host-job-one"
        assert schedule_id == "pynchy-host-job-schedule-host-job-one"
        assert schedule.spec.intervals[0].every == timedelta(seconds=60)
        assert schedule.action.workflow == "DatabaseHostJobWorkflow"
        assert schedule.action.args == [host_job.id]

    @pytest.mark.asyncio
    async def test_reconcile_creates_temporal_schedule_for_config_cron_job(self, monkeypatch):
        jobs = {
            "backup_db": JobConfig(
                schedule="15 3 * * *",
                workspace="host",
                command="scripts/backup_runtime_dbs.sh",
            )
        }
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(jobs=jobs)
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        await runtime.reconcile_schedules()

        schedules = {schedule_id: schedule for schedule_id, schedule, _ in client.created_schedules}
        schedule = schedules["pynchy-host-cron-schedule-backup_db"]
        schedule_id = "pynchy-host-cron-schedule-backup_db"
        assert schedule_id == "pynchy-host-cron-schedule-backup_db"
        assert schedule.spec.cron_expressions == ["15 3 * * *"]
        assert schedule.action.workflow == "ConfigHostCronWorkflow"
        assert schedule.action.args == ["backup_db"]

    @pytest.mark.asyncio
    async def test_reconcile_creates_schedule_for_enabled_canaries(self, monkeypatch):
        client = FakeScheduleClient()
        canary = CanaryConfig(
            enabled=True,
            target_profile="external-canary",
            schedule="30 4 * * *",
        )
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=_scheduler_runtime(canary=canary)
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        await runtime.reconcile_schedules()

        schedules = {schedule_id: schedule for schedule_id, schedule, _ in client.created_schedules}
        schedule = schedules["pynchy-canary-schedule"]
        assert schedule.spec.cron_expressions == ["30 4 * * *"]
        assert schedule.spec.time_zone_name == "UTC"
        assert schedule.action.workflow == "CanaryRunWorkflow"
        assert schedule.action.id == "pynchy-canary-schedule-workflow"

    @pytest.mark.asyncio
    async def test_quiet_success_config_host_job_suppresses_success_output_log(self, monkeypatch):
        deps = NullSchedulerDeps(
            scheduler_runtime=_scheduler_runtime(
                jobs={
                    "backup_db": JobConfig(
                        schedule="15 3 * * *",
                        workspace="host",
                        command="scripts/backup_runtime_dbs.sh",
                        quiet_on_success=True,
                    )
                }
            )
        )
        temporal_scheduler.bind_scheduler_deps(deps)
        monkeypatch.setattr(temporal_host_jobs, "_resolve_job_cwd", lambda cwd, _root: "/repo")
        monkeypatch.setattr(
            temporal_host_jobs,
            "run_shell_command",
            AsyncMock(return_value=ShellResult(returncode=0, stdout="backup ok", stderr="")),
        )
        log_shell_result = AsyncMock()
        monkeypatch.setattr(temporal_host_jobs, "log_shell_result", log_shell_result)

        result = await temporal_host_jobs.run_config_host_cron_job("backup_db")

        assert result == "completed"
        log_shell_result.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_config_host_job_fails_its_temporal_activity(self, monkeypatch):
        deps = NullSchedulerDeps(
            scheduler_runtime=_scheduler_runtime(
                jobs={
                    "backup_db": JobConfig(
                        schedule="15 3 * * *",
                        workspace="host",
                        command="scripts/backup_runtime_dbs.sh",
                    )
                }
            )
        )
        temporal_scheduler.bind_scheduler_deps(deps)
        monkeypatch.setattr(temporal_host_jobs, "_resolve_job_cwd", lambda cwd, _root: "/repo")
        monkeypatch.setattr(
            temporal_host_jobs,
            "run_shell_command",
            AsyncMock(return_value=ShellResult(returncode=1, stdout="", stderr="boom")),
        )

        with pytest.raises(RuntimeError, match="Host job backup_db exited with code 1"):
            await temporal_host_jobs.run_config_host_cron_job("backup_db")

    @pytest.mark.asyncio
    async def test_config_host_job_memory_opt_out(self, monkeypatch):
        deps = NullSchedulerDeps(
            scheduler_runtime=_scheduler_runtime(
                jobs={
                    "backup_db": JobConfig(
                        schedule="15 3 * * *",
                        workspace="host",
                        command="scripts/backup_runtime_dbs.sh",
                        memory=False,
                    )
                }
            )
        )
        temporal_scheduler.bind_scheduler_deps(deps)
        memory_context = Mock(side_effect=AssertionError("memory directory must stay disabled"))
        monkeypatch.setattr(deps, "automation_memory_dir", memory_context)
        mock_shell = AsyncMock(return_value=ShellResult(returncode=0, stdout="", stderr=""))
        monkeypatch.setattr(temporal_host_jobs, "run_shell_command", mock_shell)

        assert await temporal_host_jobs.run_config_host_cron_job("backup_db") == "completed"

        assert mock_shell.await_args.kwargs["env"] is None
        memory_context.assert_not_called()

    @pytest.mark.asyncio
    async def test_reconcile_creates_temporal_schedules_for_git_sync_and_channel_reconcile(
        self, monkeypatch, tmp_path
    ):
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=_scheduler_runtime(
                project_root=tmp_path / "pynchy",
                external_repo_sync_slugs=("owner/project",),
            ),
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))

        await runtime.reconcile_schedules()

        schedules = {schedule_id: schedule for schedule_id, schedule, _ in client.created_schedules}
        assert schedules["pynchy-git-sync-host"].action.workflow == "HostGitSyncWorkflow"
        assert schedules["pynchy-git-sync-host"].spec.intervals[0].every == timedelta(minutes=5)
        assert schedules["pynchy-git-sync-repo-owner-project"].action.workflow == (
            "ExternalGitSyncWorkflow"
        )
        assert schedules["pynchy-git-sync-repo-owner-project"].action.args == ["owner/project"]
        assert schedules["pynchy-channel-reconciliation"].action.workflow == (
            "ChannelReconciliationWorkflow"
        )
        assert schedules["pynchy-channel-reconciliation"].spec.intervals[0].every == (
            timedelta(minutes=5)
        )
        work_items = schedules["pynchy-linear-work-item-reconciliation"]
        assert work_items.action.workflow == "LinearWorkItemReconciliationWorkflow"
        assert work_items.spec.intervals[0].every == timedelta(minutes=1)
        assert work_items.policy.catchup_window == timedelta(minutes=1)

    def test_poller_schedules_bound_default_catchup_to_their_intervals(self):
        runtime = _scheduler_runtime(external_repo_sync_slugs=("owner/project",))
        desired = temporal_schedules.desired_recurring_schedules([], [], runtime)
        schedules = (
            desired["pynchy-git-sync-host"],
            desired["pynchy-git-sync-repo-owner-project"],
            desired["pynchy-channel-reconciliation"],
            desired["pynchy-linear-work-item-reconciliation"],
        )

        expected_intervals = (
            timedelta(minutes=5),
            timedelta(minutes=5),
            timedelta(minutes=5),
            timedelta(minutes=1),
        )
        for schedule, interval in zip(schedules, expected_intervals, strict=True):
            assert schedule.spec.intervals[0].every == interval
            assert schedule.policy.catchup_window == interval
