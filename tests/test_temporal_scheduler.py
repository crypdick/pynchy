"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from conftest import make_settings

from pynchy.config import CronJobConfig, ProfileConfig, RepoConfig, SchedulerConfig, WorkspaceConfig
from pynchy.config.models import ReposConfig
from pynchy.host.learning.packet_codec import packet_to_payload
from pynchy.host.learning.packet_models import LearningPacket
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.types import HostJob, ScheduledTask
from pynchy.utils import ShellResult


@dataclass
class NullSchedulerDeps:
    """Structural fake for SchedulerDependencies."""

    queue: GroupQueue = field(default_factory=GroupQueue)

    def workspaces(self):
        return {}

    async def broadcast_to_channels(self, jid, event) -> None: ...

    async def broadcast_host_message(self, chat_jid, text) -> None: ...

    async def broadcast_system_notice(self, chat_jid, text) -> None: ...

    async def run_agent(self, *args, **kwargs) -> str:
        return "success"

    async def handle_streamed_output(self, chat_jid, group, result) -> bool:
        return False


class FakeScheduleHandle:
    def __init__(self, schedule_id: str):
        self.schedule_id = schedule_id
        self.updates = []
        self.deleted = False

    async def update(self, updater):
        update = updater(SimpleNamespace(description=None))
        self.updates.append(update)

    async def delete(self):
        self.deleted = True


class FakeScheduleClient:
    def __init__(self):
        self.created_schedules = []
        self.started_workflows = []
        self.handles = {}
        self.schedule_ids = []

    async def create_schedule(self, schedule_id, schedule, **kwargs):
        self.created_schedules.append((schedule_id, schedule, kwargs))
        return self.handles.setdefault(schedule_id, FakeScheduleHandle(schedule_id))

    def get_schedule_handle(self, schedule_id):
        return self.handles.setdefault(schedule_id, FakeScheduleHandle(schedule_id))

    def list_schedules(self):
        return _ScheduleIterator(self.schedule_ids)

    async def start_workflow(self, workflow, *posargs, **kwargs):
        assert len(posargs) <= 1
        workflow_args = tuple(kwargs.pop("args", ()))
        assert not (posargs and workflow_args)
        self.started_workflows.append((workflow, posargs or workflow_args, kwargs))


class AwaitableScheduleListClient(FakeScheduleClient):
    def list_schedules(self):
        return asyncio.sleep(0, result=super().list_schedules())


class _ScheduleIterator:
    def __init__(self, schedule_ids):
        self._schedule_ids = iter(schedule_ids)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        try:
            schedule_id = next(self._schedule_ids)
        except StopIteration:
            raise StopAsyncIteration from None
        return SimpleNamespace(id=schedule_id)


@pytest.fixture
def learning_packet() -> LearningPacket:
    return LearningPacket(
        job_id="learning job one",
        chat_jid="slack:C123",
        group_folder="research",
        profile="Deep Work",
        created_at="2026-07-07T10:00:00+00:00",
        messages=[{"role": "user", "content": "remember this workflow"}],
        final_answer="Done.",
        tool_counts={"Bash": 1},
        error_snippets=[],
        loaded_skills=[],
        provenance={"run_id": "run-123"},
    )


@pytest.fixture
def temporal_task() -> ScheduledTask:
    return ScheduledTask(
        id="task/with spaces",
        group_folder="test-group",
        chat_jid="test@g.us",
        prompt="Test task",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="isolated",
        next_run=datetime(2026, 7, 7, 9, 0, tzinfo=UTC).isoformat(),
        status="active",
    )


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
        from pynchy.host.orchestrator.temporal.workflows import (
            ChannelReconciliationWorkflow,
            DeployWorkflow,
            ExternalGitSyncWorkflow,
            HostGitSyncWorkflow,
            InteractiveMessageWorkflow,
            LearningReviewWorkflow,
        )

        assert {
            InteractiveMessageWorkflow,
            LearningReviewWorkflow,
            DeployWorkflow,
            HostGitSyncWorkflow,
            ExternalGitSyncWorkflow,
            ChannelReconciliationWorkflow,
        }.issubset(set(captured["workflows"]))
        assert {
            temporal_scheduler.run_interactive_message_turn,
            temporal_scheduler.run_learning_review,
            temporal_scheduler.run_deploy,
            temporal_scheduler.run_host_git_sync,
            temporal_scheduler.run_external_git_sync,
            temporal_scheduler.run_channel_reconciliation,
        }.issubset(set(captured["activities"]))

    @staticmethod
    def _assert_finalize_deploy_kwargs(finalize_deploy: AsyncMock, deps: NullSchedulerDeps) -> None:
        finalize_deploy.assert_awaited_once()
        assert finalize_deploy.await_args.kwargs == {
            "broadcast_host_message": deps.broadcast_host_message,
            "chat_jid": "slack:C123",
            "commit_sha": "new-sha",
            "previous_sha": "old-sha",
            "session_id": "session-1",
            "active_sessions": {"slack:C123": "session-1"},
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

    def test_scheduler_status_defaults_to_stopped(self):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        temporal_scheduler.reset_temporal_scheduler_status()

        status = temporal_scheduler.get_temporal_scheduler_status()

        assert status == {
            "worker_running": False,
            "last_workflow_id": None,
            "last_task_id": None,
            "last_result": None,
            "last_started_at": None,
            "last_completed_at": None,
            "last_error": None,
        }

    def test_scheduler_workflow_runner_passes_through_workflow_module(self):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        runner = temporal_scheduler.scheduler_workflow_runner()

        assert (
            "pynchy.host.orchestrator.temporal.workflows" in runner.restrictions.passthrough_modules
        )

    def test_agent_task_workflow_id_is_stable_and_temporal_safe(self, temporal_task):
        from pynchy.host.orchestrator.temporal.scheduler import agent_task_workflow_id

        workflow_id = agent_task_workflow_id(temporal_task)

        assert workflow_id == "pynchy-agent-task-task-with-spaces-2026-07-07T09-00-00-00-00"

    def test_agent_task_schedule_id_is_stable_and_temporal_safe(self, temporal_task):
        from pynchy.host.orchestrator.temporal.scheduler import agent_task_schedule_id

        schedule_id = agent_task_schedule_id(temporal_task)

        assert schedule_id == "pynchy-agent-schedule-task-with-spaces"

    def test_learning_review_workflow_id_is_stable_and_temporal_safe(self, learning_packet):
        from pynchy.host.orchestrator.temporal.scheduler import learning_review_workflow_id

        workflow_id = learning_review_workflow_id(learning_packet)

        assert workflow_id == "pynchy-learning-review-learning-job-one"

    def test_interactive_message_workflow_id_is_stable_and_temporal_safe(self):
        from pynchy.host.orchestrator.temporal.scheduler import interactive_message_workflow_id

        workflow_id = interactive_message_workflow_id("slack:C123/with spaces")

        assert workflow_id == "pynchy-interactive-turn-slack-C123-with-spaces"

    def test_deploy_workflow_id_is_stable_and_temporal_safe(self):
        from pynchy.host.orchestrator.temporal.scheduler import deploy_workflow_id

        workflow_id = deploy_workflow_id("abc1234/with spaces")

        assert workflow_id == "pynchy-deploy-abc1234-with-spaces"

    @pytest.mark.asyncio
    async def test_start_scheduled_agent_task_uses_configured_task_queue(self, temporal_task):
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def start_workflow(
                self,
                workflow,
                *posargs,
                workflow_id=None,
                task_queue,
                id_reuse_policy,
                args=(),
                **kwargs,
            ):
                assert len(posargs) <= 1
                workflow_args = tuple(args)
                assert not (posargs and workflow_args)
                resolved_workflow_id = kwargs.get("id", workflow_id)
                assert resolved_workflow_id is not None
                self.calls.append(
                    {
                        "workflow": workflow,
                        "args": posargs or workflow_args,
                        "id": resolved_workflow_id,
                        "task_queue": task_queue,
                        "id_reuse_policy": id_reuse_policy,
                    }
                )

        scheduler = SchedulerConfig(
            temporal_address="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="pynchy-test",
        )
        runtime = TemporalSchedulerRuntime(deps=NullSchedulerDeps(), scheduler_config=scheduler)
        runtime.client = FakeClient()

        await runtime.start_scheduled_agent_task(temporal_task)

        assert len(runtime.client.calls) == 1
        call = runtime.client.calls[0]
        assert call["args"] == (temporal_task.id,)
        assert call["id"] == "pynchy-agent-task-task-with-spaces-2026-07-07T09-00-00-00-00"
        assert call["task_queue"] == "pynchy-test"
        assert call["id_reuse_policy"].name == "REJECT_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_scheduled_agent_task_updates_status(self, temporal_task):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        class FakeClient:
            async def start_workflow(
                self,
                workflow,
                *posargs,
                workflow_id=None,
                task_queue,
                id_reuse_policy,
                args=(),
                **kwargs,
            ):
                assert len(posargs) <= 1
                assert not (posargs and args)
                assert kwargs.get("id", workflow_id) is not None

        temporal_scheduler.reset_temporal_scheduler_status()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=scheduler
        )
        runtime.client = FakeClient()

        await runtime.start_scheduled_agent_task(temporal_task)

        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == (
            "pynchy-agent-task-task-with-spaces-2026-07-07T09-00-00-00-00"
        )
        assert status["last_task_id"] == temporal_task.id
        assert status["last_result"] == "started"
        assert status["last_started_at"] is not None
        assert status["last_completed_at"] is None

    @pytest.mark.asyncio
    async def test_start_learning_review_starts_temporal_workflow(self, learning_packet):
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime
        from pynchy.host.orchestrator.temporal.workflows import LearningReviewWorkflow

        client = FakeScheduleClient()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = TemporalSchedulerRuntime(deps=NullSchedulerDeps(), scheduler_config=scheduler)
        runtime.client = client

        await runtime.start_learning_review(learning_packet)

        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == LearningReviewWorkflow.run
        assert args == (packet_to_payload(learning_packet), 3)
        assert kwargs["id"] == "pynchy-learning-review-learning-job-one"
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "REJECT_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_interactive_message_turn_starts_temporal_workflow(self):
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime
        from pynchy.host.orchestrator.temporal.workflows import InteractiveMessageWorkflow

        client = FakeScheduleClient()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = TemporalSchedulerRuntime(deps=NullSchedulerDeps(), scheduler_config=scheduler)
        runtime.client = client

        await runtime.start_interactive_message_turn("slack:C123")

        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == InteractiveMessageWorkflow.run
        assert args == ("slack:C123", 6, 5.0)
        assert kwargs["id"] == "pynchy-interactive-turn-slack-C123"
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE"

    @pytest.mark.asyncio
    async def test_start_deploy_starts_temporal_workflow(self):
        from pynchy.host.orchestrator.temporal.deploy import (
            DeployRequest,
            deploy_request_to_payload,
        )
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime
        from pynchy.host.orchestrator.temporal.workflows import DeployWorkflow

        request = DeployRequest(
            chat_jid="slack:C123",
            commit_sha="abc123",
            previous_sha="def456",
            active_sessions={"slack:C123": "session-1"},
            rebuild=True,
            reason="origin",
        )
        client = FakeScheduleClient()
        scheduler = SchedulerConfig(temporal_task_queue="pynchy-test")
        runtime = TemporalSchedulerRuntime(deps=NullSchedulerDeps(), scheduler_config=scheduler)
        runtime.client = client

        await runtime.start_deploy(request)

        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == DeployWorkflow.run
        assert args == (deploy_request_to_payload(request),)
        assert kwargs["id"] == "pynchy-deploy-abc123"
        assert kwargs["task_queue"] == "pynchy-test"
        assert kwargs["id_reuse_policy"].name == "ALLOW_DUPLICATE"

    @pytest.mark.asyncio
    async def test_reconcile_creates_temporal_schedule_for_recurring_agent_task(
        self, monkeypatch, temporal_task
    ):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        import pynchy.host.orchestrator.temporal.schedules as temporal_schedules

        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), cron_jobs={})
        monkeypatch.setattr(temporal_scheduler, "get_settings", lambda: settings)
        monkeypatch.setattr(temporal_schedules, "get_settings", lambda: settings)

        await runtime.reconcile_schedules()

        schedules = {
            schedule_id: (schedule, kwargs)
            for schedule_id, schedule, kwargs in client.created_schedules
        }
        schedule, kwargs = schedules["pynchy-agent-schedule-task-with-spaces"]
        schedule_id = "pynchy-agent-schedule-task-with-spaces"
        assert schedule_id == "pynchy-agent-schedule-task-with-spaces"
        assert schedule.spec.cron_expressions == ["0 9 * * *"]
        assert schedule.spec.time_zone_name == "UTC"
        assert schedule.action.workflow == "ScheduledAgentTaskWorkflow"
        assert schedule.action.args == [temporal_task.id]
        assert schedule.action.id == "pynchy-agent-schedule-task-with-spaces-workflow"
        assert kwargs == {}

    @pytest.mark.asyncio
    async def test_reconcile_starts_once_agent_task_as_delayed_workflow(
        self, monkeypatch, temporal_task
    ):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        import pynchy.host.orchestrator.temporal.schedules as temporal_schedules
        from pynchy.host.orchestrator.temporal.workflows import ScheduledAgentTaskWorkflow

        temporal_task.schedule_type = "once"
        temporal_task.schedule_value = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        temporal_task.next_run = temporal_task.schedule_value
        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(),
            scheduler_config=SchedulerConfig(temporal_task_queue="pynchy-test"),
        )
        runtime.client = client
        monkeypatch.setattr(
            temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[temporal_task])
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(timezone="UTC", scheduler=runtime.scheduler_config, cron_jobs={})
        monkeypatch.setattr(temporal_scheduler, "get_settings", lambda: settings)
        monkeypatch.setattr(temporal_schedules, "get_settings", lambda: settings)

        await runtime.reconcile_schedules()

        assert all(
            schedule_id.startswith(("pynchy-git-sync-", "pynchy-channel-reconciliation"))
            for schedule_id, _schedule, _kwargs in client.created_schedules
        )
        assert len(client.started_workflows) == 1
        workflow, args, kwargs = client.started_workflows[0]
        assert workflow == ScheduledAgentTaskWorkflow.run
        assert args == (temporal_task.id,)
        assert kwargs["id"].startswith("pynchy-agent-task-task-with-spaces-")
        assert kwargs["task_queue"] == "pynchy-test"
        assert 0 < kwargs["start_delay"].total_seconds() <= 300

    @pytest.mark.asyncio
    async def test_reconcile_creates_temporal_schedule_for_database_host_job(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        import pynchy.host.orchestrator.temporal.schedules as temporal_schedules

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
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(
            temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[host_job])
        )
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), cron_jobs={})
        monkeypatch.setattr(temporal_scheduler, "get_settings", lambda: settings)
        monkeypatch.setattr(temporal_schedules, "get_settings", lambda: settings)

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
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        import pynchy.host.orchestrator.temporal.schedules as temporal_schedules

        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )
        runtime.client = client
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        settings = make_settings(
            timezone="UTC",
            scheduler=SchedulerConfig(),
            cron_jobs={
                "backup_db": CronJobConfig(
                    schedule="15 3 * * *",
                    command="scripts/backup_runtime_dbs.sh",
                )
            },
        )
        monkeypatch.setattr(temporal_scheduler, "get_settings", lambda: settings)
        monkeypatch.setattr(temporal_schedules, "get_settings", lambda: settings)

        await runtime.reconcile_schedules()

        schedules = {schedule_id: schedule for schedule_id, schedule, _ in client.created_schedules}
        schedule = schedules["pynchy-host-cron-schedule-backup_db"]
        schedule_id = "pynchy-host-cron-schedule-backup_db"
        assert schedule_id == "pynchy-host-cron-schedule-backup_db"
        assert schedule.spec.cron_expressions == ["15 3 * * *"]
        assert schedule.action.workflow == "ConfigHostCronWorkflow"
        assert schedule.action.args == ["backup_db"]

    @pytest.mark.asyncio
    async def test_quiet_success_config_host_job_suppresses_success_output_log(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.host_jobs as temporal_host_jobs

        settings = make_settings(
            cron_jobs={
                "backup_db": CronJobConfig(
                    schedule="15 3 * * *",
                    command="scripts/backup_runtime_dbs.sh",
                    quiet_on_success=True,
                )
            },
        )
        monkeypatch.setattr(temporal_host_jobs, "get_settings", lambda: settings)
        monkeypatch.setattr(temporal_host_jobs, "resolve_cron_job_cwd", lambda cwd: "/repo")
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
    async def test_reconcile_creates_temporal_schedules_for_git_sync_and_channel_reconcile(
        self, monkeypatch, tmp_path
    ):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        import pynchy.host.orchestrator.temporal.schedules as temporal_schedules

        client = FakeScheduleClient()
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )
        runtime.client = client
        repo_root = tmp_path / "external"
        settings = make_settings(
            project_root=tmp_path / "pynchy",
            timezone="UTC",
            scheduler=SchedulerConfig(),
            cron_jobs={},
            repos=ReposConfig(overrides={"owner/project": RepoConfig(path=str(repo_root))}),
            profiles={"worker": ProfileConfig(repo="owner/project")},
            workspaces={"worker": WorkspaceConfig(profiles=["worker"])},
        )
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_settings", lambda: settings)
        monkeypatch.setattr(temporal_schedules, "get_settings", lambda: settings)

        await runtime.reconcile_schedules()

        schedules = {schedule_id: schedule for schedule_id, schedule, _ in client.created_schedules}
        assert schedules["pynchy-git-sync-host"].action.workflow == "HostGitSyncWorkflow"
        assert schedules["pynchy-git-sync-host"].spec.intervals[0].every == timedelta(seconds=5)
        assert schedules["pynchy-git-sync-repo-owner-project"].action.workflow == (
            "ExternalGitSyncWorkflow"
        )
        assert schedules["pynchy-git-sync-repo-owner-project"].action.args == ["owner/project"]
        assert schedules["pynchy-channel-reconciliation"].action.workflow == (
            "ChannelReconciliationWorkflow"
        )
        assert schedules["pynchy-channel-reconciliation"].spec.intervals[0].every == (
            timedelta(seconds=10)
        )

    @pytest.mark.asyncio
    async def test_reconcile_accepts_awaitable_schedule_list(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        import pynchy.host.orchestrator.temporal.schedules as temporal_schedules

        client = AwaitableScheduleListClient()
        stale_schedule_id = "pynchy-agent-schedule-stale"
        client.schedule_ids = [stale_schedule_id]
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )
        runtime.client = client
        settings = make_settings(timezone="UTC", scheduler=SchedulerConfig(), cron_jobs={})
        monkeypatch.setattr(temporal_scheduler, "get_all_tasks", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_all_host_jobs", AsyncMock(return_value=[]))
        monkeypatch.setattr(temporal_scheduler, "get_settings", lambda: settings)
        monkeypatch.setattr(temporal_schedules, "get_settings", lambda: settings)

        await runtime.reconcile_schedules()

        assert client.handles[stale_schedule_id].deleted is True

    @pytest.mark.asyncio
    async def test_worker_lifecycle_updates_running_status(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        def fake_connect(*args, **kwargs):
            return asyncio.sleep(0, result=object())

        @asynccontextmanager
        async def fake_worker(*args, **kwargs):
            yield object()

        temporal_scheduler.reset_temporal_scheduler_status()
        monkeypatch.setattr(temporal_scheduler.Client, "connect", fake_connect)
        monkeypatch.setattr(temporal_scheduler, "Worker", fake_worker)
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )

        async with runtime:
            assert temporal_scheduler.get_temporal_scheduler_status()["worker_running"] is True

        assert temporal_scheduler.get_temporal_scheduler_status()["worker_running"] is False

    @pytest.mark.asyncio
    async def test_worker_registers_temporal_orchestration_workflows(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        captured = {}

        def fake_connect(*args, **kwargs):
            return asyncio.sleep(0, result=object())

        monkeypatch.setattr(temporal_scheduler.Client, "connect", fake_connect)
        monkeypatch.setattr(temporal_scheduler, "Worker", self._capturing_worker(captured))
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )

        async with runtime:
            self._assert_registered_temporal_workflows(captured, temporal_scheduler)

    @pytest.mark.asyncio
    async def test_startup_failure_unbinds_scheduler_deps(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        def fail_connect(*args, **kwargs):
            raise RuntimeError("temporal unavailable")

        scheduler = SchedulerConfig(
            temporal_address="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="pynchy-test",
        )
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=scheduler
        )

        monkeypatch.setattr(temporal_scheduler.Client, "connect", fail_connect)

        with pytest.raises(RuntimeError, match="temporal unavailable"):
            async with runtime:
                pass

        with pytest.raises(RuntimeError, match="dependencies are not bound"):
            temporal_scheduler._require_scheduler_deps()

        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["worker_running"] is False
        assert status["last_error"] == "temporal unavailable"

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_uses_shared_runner(
        self, monkeypatch, temporal_task
    ):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        deps = NullSchedulerDeps()
        called = {}

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task if task_id == temporal_task.id else None)

        def fake_run_scheduled_agent(task, runner_deps):
            called["task"] = task
            called["deps"] = runner_deps
            return asyncio.sleep(0, result=None)

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(temporal_scheduler, "_run_scheduled_agent", fake_run_scheduled_agent)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="workflow-completed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        assert result == "completed"
        assert called == {"task": temporal_task, "deps": deps}
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "workflow-completed"
        assert status["last_task_id"] == temporal_task.id
        assert status["last_result"] == "completed"
        assert status["last_completed_at"] is not None

    @pytest.mark.asyncio
    async def test_run_learning_review_activity_uses_bound_deps(self, monkeypatch, learning_packet):
        import pynchy.host.orchestrator.temporal.learning as temporal_learning
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        deps = NullSchedulerDeps()
        called = {}

        def fake_run_learning_review(packet, runner_deps):
            called["packet"] = packet
            called["deps"] = runner_deps
            return asyncio.sleep(0, result="completed")

        monkeypatch.setattr(temporal_learning, "_run_learning_review", fake_run_learning_review)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="learning-workflow-completed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_scheduler.run_learning_review(packet_to_payload(learning_packet))

        assert result == "completed"
        assert called == {"packet": learning_packet, "deps": deps}
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "learning-workflow-completed"
        assert status["last_task_id"] == learning_packet.job_id
        assert status["last_result"] == "completed"

    @pytest.mark.asyncio
    async def test_run_interactive_message_activity_uses_bound_deps(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.interactive as temporal_interactive
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        deps = NullSchedulerDeps()
        called = {}

        def fake_process_message_turn(runner_deps, chat_jid):
            called["deps"] = runner_deps
            called["chat_jid"] = chat_jid
            return asyncio.sleep(0, result=True)

        monkeypatch.setattr(
            temporal_interactive, "_process_interactive_message_turn", fake_process_message_turn
        )
        monkeypatch.setattr(
            temporal_interactive.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="interactive-workflow-completed"),
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
    async def test_run_interactive_message_activity_retries_unhandled_turn(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.interactive as temporal_interactive
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        def fake_process_message_turn(_runner_deps, _chat_jid):
            return asyncio.sleep(0, result=False)

        monkeypatch.setattr(
            temporal_interactive, "_process_interactive_message_turn", fake_process_message_turn
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(RuntimeError, match="Interactive message turn requested retry"):
            await temporal_scheduler.run_interactive_message_turn("slack:C123")

    @pytest.mark.asyncio
    async def test_run_deploy_activity_builds_then_finalizes(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.deploy as temporal_deploy
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        from pynchy.host.orchestrator.deploy import BuildResult
        from pynchy.host.orchestrator.temporal.deploy import DeployRequest

        deps = NullSchedulerDeps()
        deps.broadcast_host_message = AsyncMock()
        finalize_deploy = AsyncMock()
        request = DeployRequest(
            chat_jid="slack:C123",
            commit_sha="new-sha",
            previous_sha="old-sha",
            session_id="session-1",
            active_sessions={"slack:C123": "session-1"},
            rebuild=True,
            reason="test",
        )

        monkeypatch.setattr(
            temporal_deploy,
            "build_container_image",
            lambda: BuildResult(success=True),
        )
        monkeypatch.setattr(temporal_deploy, "finalize_deploy", finalize_deploy)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="deploy-workflow-completed"),
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
        import pynchy.host.orchestrator.temporal.deploy as temporal_deploy
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        from pynchy.host.orchestrator.deploy import BuildResult
        from pynchy.host.orchestrator.temporal.deploy import DeployRequest

        deps = NullSchedulerDeps()
        deps.broadcast_host_message = AsyncMock()
        finalize_deploy = AsyncMock()
        request = DeployRequest(
            chat_jid="slack:C123",
            commit_sha="new-sha",
            previous_sha="old-sha",
            rebuild=True,
        )

        monkeypatch.setattr(
            temporal_deploy,
            "build_container_image",
            lambda: BuildResult(success=False, stderr="image build exploded"),
        )
        monkeypatch.setattr(temporal_deploy, "finalize_deploy", finalize_deploy)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="deploy-workflow-failed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_deploy.run_deploy(
            temporal_deploy.deploy_request_to_payload(request)
        )

        assert result == "build_failed"
        finalize_deploy.assert_not_awaited()
        deps.broadcast_host_message.assert_awaited_once_with(
            "slack:C123",
            "Deploy failed: Container rebuild failed: image build exploded",
        )
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "deploy-workflow-failed"
        assert status["last_task_id"] == "new-sha"
        assert status["last_result"] == "build_failed"
        assert status["last_error"] == "Container rebuild failed: image build exploded"

    @pytest.mark.asyncio
    async def test_run_channel_reconciliation_activity_uses_bound_deps(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.channel_reconciliation as temporal_channels
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        deps = NullSchedulerDeps()
        called = {}

        def fake_reconcile_all_channels(runner_deps):
            called["deps"] = runner_deps
            return asyncio.sleep(0, result=None)

        monkeypatch.setattr(
            temporal_channels,
            "reconcile_all_channels",
            fake_reconcile_all_channels,
        )
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="channel-reconcile-completed"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(deps)

        result = await temporal_channels.run_channel_reconciliation()

        assert result == "completed"
        assert called == {"deps": deps}
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "channel-reconcile-completed"
        assert status["last_task_id"] == "channel-reconciliation"
        assert status["last_result"] == "completed"

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_skips_paused_task(self, monkeypatch, temporal_task):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        temporal_task.status = "paused"

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task)

        def fake_run_scheduled_agent(task, runner_deps):
            raise AssertionError("paused tasks must not run")

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(temporal_scheduler, "_run_scheduled_agent", fake_run_scheduled_agent)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="workflow-skipped"),
        )
        temporal_scheduler.reset_temporal_scheduler_status()
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        result = await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

        assert result == "skipped"
        status = temporal_scheduler.get_temporal_scheduler_status()
        assert status["last_workflow_id"] == "workflow-skipped"
        assert status["last_result"] == "skipped"

    @pytest.mark.asyncio
    async def test_run_scheduled_agent_activity_retries_failed_runner(
        self, monkeypatch, temporal_task
    ):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task)

        def fake_run_scheduled_agent(task, runner_deps):
            return asyncio.sleep(0, result=False)

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(temporal_scheduler, "_run_scheduled_agent", fake_run_scheduled_agent)
        monkeypatch.setattr(
            temporal_scheduler.activity,
            "info",
            lambda: SimpleNamespace(workflow_id="workflow-failed"),
        )
        temporal_scheduler.bind_scheduler_deps(NullSchedulerDeps())

        with pytest.raises(RuntimeError, match="Scheduled agent task requested retry"):
            await temporal_scheduler.run_scheduled_agent_task(temporal_task.id)

    @pytest.mark.live
    @pytest.mark.asyncio
    async def test_workflow_executes_activity_through_temporal_worker(
        self, monkeypatch, temporal_task
    ):
        """Temporal can run the Pynchy workflow and activity path end to end."""
        from temporalio.testing import WorkflowEnvironment
        from temporalio.worker import Worker

        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        from pynchy.host.orchestrator.temporal.workflows import ScheduledAgentTaskWorkflow

        deps = NullSchedulerDeps()
        called = {}
        task_queue = f"pynchy-temporal-test-{uuid4()}"

        def fake_get_task_by_id(task_id: str):
            return asyncio.sleep(0, result=temporal_task if task_id == temporal_task.id else None)

        def fake_run_scheduled_agent(task, runner_deps):
            called["task_id"] = task.id
            called["deps"] = runner_deps
            return asyncio.sleep(0, result=None)

        monkeypatch.setattr(temporal_scheduler, "get_task_by_id", fake_get_task_by_id)
        monkeypatch.setattr(temporal_scheduler, "_run_scheduled_agent", fake_run_scheduled_agent)
        temporal_scheduler.bind_scheduler_deps(deps)

        env = await WorkflowEnvironment.start_time_skipping()
        try:
            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[ScheduledAgentTaskWorkflow],
                activities=[temporal_scheduler.run_scheduled_agent_task],
                workflow_runner=temporal_scheduler.scheduler_workflow_runner(),
            ):
                result = await env.client.execute_workflow(
                    ScheduledAgentTaskWorkflow.run,
                    temporal_task.id,
                    id=f"pynchy-temporal-test-{uuid4()}",
                    task_queue=task_queue,
                )
        finally:
            temporal_scheduler.bind_scheduler_deps(None)
            await env.shutdown()

        assert result == "completed"
        assert called == {"task_id": temporal_task.id, "deps": deps}
