"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from conftest import make_settings

from pynchy.config import CronJobConfig, SchedulerConfig
from pynchy.host.learning.packet_codec import packet_to_payload
from pynchy.host.learning.packet_models import LearningPacket
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.types import HostJob, ScheduledTask


class NullSchedulerDeps:
    """Structural fake for SchedulerDependencies."""

    def __init__(self):
        self.queue = GroupQueue()

    def workspaces(self):
        return {}

    async def broadcast_to_channels(self, jid, event) -> None: ...

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
        handle = self.handles.setdefault(schedule_id, FakeScheduleHandle(schedule_id))
        return handle

    def get_schedule_handle(self, schedule_id):
        return self.handles.setdefault(schedule_id, FakeScheduleHandle(schedule_id))

    def list_schedules(self):
        async def _iter():
            for schedule_id in self.schedule_ids:
                yield SimpleNamespace(id=schedule_id)

        return _iter()

    async def start_workflow(self, workflow, *args, **kwargs):
        self.started_workflows.append((workflow, args, kwargs))


class AwaitableScheduleListClient(FakeScheduleClient):
    async def list_schedules(self):
        return super().list_schedules()


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

    @pytest.mark.asyncio
    async def test_start_scheduled_agent_task_uses_configured_task_queue(self, temporal_task):
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def start_workflow(self, workflow, *args, id, task_queue, id_reuse_policy):
                self.calls.append(
                    {
                        "workflow": workflow,
                        "args": args,
                        "id": id,
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
            async def start_workflow(self, workflow, *args, id, task_queue, id_reuse_policy):
                return None

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

        assert len(client.created_schedules) == 1
        schedule_id, schedule, kwargs = client.created_schedules[0]
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

        assert client.created_schedules == []
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

        schedule_id, schedule, _kwargs = client.created_schedules[0]
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

        schedule_id, schedule, _kwargs = client.created_schedules[0]
        assert schedule_id == "pynchy-host-cron-schedule-backup_db"
        assert schedule.spec.cron_expressions == ["15 3 * * *"]
        assert schedule.action.workflow == "ConfigHostCronWorkflow"
        assert schedule.action.args == ["backup_db"]

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

        class FakeWorker:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        async def fake_connect(*args, **kwargs):
            return object()

        temporal_scheduler.reset_temporal_scheduler_status()
        monkeypatch.setattr(temporal_scheduler.Client, "connect", fake_connect)
        monkeypatch.setattr(temporal_scheduler, "Worker", FakeWorker)
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )

        await runtime.__aenter__()
        assert temporal_scheduler.get_temporal_scheduler_status()["worker_running"] is True

        await runtime.__aexit__(None, None, None)
        assert temporal_scheduler.get_temporal_scheduler_status()["worker_running"] is False

    @pytest.mark.asyncio
    async def test_worker_registers_learning_review_workflow_and_activity(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler
        from pynchy.host.orchestrator.temporal.workflows import LearningReviewWorkflow

        captured = {}

        class FakeWorker:
            def __init__(self, *args, workflows, activities, **kwargs):
                captured["workflows"] = workflows
                captured["activities"] = activities

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

        async def fake_connect(*args, **kwargs):
            return object()

        monkeypatch.setattr(temporal_scheduler.Client, "connect", fake_connect)
        monkeypatch.setattr(temporal_scheduler, "Worker", FakeWorker)
        runtime = temporal_scheduler.TemporalSchedulerRuntime(
            deps=NullSchedulerDeps(), scheduler_config=SchedulerConfig()
        )

        await runtime.__aenter__()

        assert LearningReviewWorkflow in captured["workflows"]
        assert temporal_scheduler.run_learning_review in captured["activities"]

        await runtime.__aexit__(None, None, None)

    @pytest.mark.asyncio
    async def test_startup_failure_unbinds_scheduler_deps(self, monkeypatch):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        async def fail_connect(*args, **kwargs):
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
            await runtime.__aenter__()

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

        async def fake_get_task_by_id(task_id: str):
            return temporal_task if task_id == temporal_task.id else None

        async def fake_run_scheduled_agent(task, runner_deps):
            called["task"] = task
            called["deps"] = runner_deps

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
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        deps = NullSchedulerDeps()
        called = {}

        async def fake_run_learning_review(packet, runner_deps):
            called["packet"] = packet
            called["deps"] = runner_deps
            return "completed"

        monkeypatch.setattr(temporal_scheduler, "_run_learning_review", fake_run_learning_review)
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
    async def test_run_scheduled_agent_activity_skips_paused_task(self, monkeypatch, temporal_task):
        import pynchy.host.orchestrator.temporal.scheduler as temporal_scheduler

        temporal_task.status = "paused"

        async def fake_get_task_by_id(task_id: str):
            return temporal_task

        async def fake_run_scheduled_agent(task, runner_deps):
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

        async def fake_get_task_by_id(task_id: str):
            return temporal_task if task_id == temporal_task.id else None

        async def fake_run_scheduled_agent(task, runner_deps):
            called["task_id"] = task.id
            called["deps"] = runner_deps

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
