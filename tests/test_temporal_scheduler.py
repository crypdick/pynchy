"""Tests for the Temporal scheduler control-plane integration."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pynchy.config import SchedulerConfig
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.types import ScheduledTask


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
