"""Tests for task scheduler.

Tests the scheduled task execution logic, including:
- Scheduler loop initialization and duplicate prevention
- Temporal reconciliation handoff
- Task execution with different context modes
- Next run calculation for cron, interval, and once schedules
- Error handling and logging
- Group lookup and validation
"""

from __future__ import annotations

# ruff: noqa: SIM117
import asyncio
import contextlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest
from conftest import make_settings

from pynchy.config import CronJobConfig, SchedulerConfig
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.host.orchestrator.task_scheduler import start_scheduler_loop
from pynchy.types import (
    ContainerOutput,
    ScheduledTask,
    TaskRunLog,
    WorkspaceProfile,
)


@contextlib.contextmanager
def _patch_settings(*, poll_interval: float = 5.0, groups_dir=None, cron_jobs=None):
    overrides = {
        "scheduler": SchedulerConfig(poll_interval=poll_interval),
        "cron_jobs": cron_jobs or {},
    }
    if groups_dir is not None:
        overrides["groups_dir"] = groups_dir
    s = make_settings(**overrides)
    with patch("pynchy.host.orchestrator.task_scheduler.get_settings", return_value=s):
        yield


class RecordingTemporalRuntime:
    instances = []

    def __init__(self, deps, scheduler_config):
        self.deps = deps
        self.scheduler_config = scheduler_config
        self.reconcile_count = 0
        RecordingTemporalRuntime.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, _tb):
        return None

    async def reconcile_schedules(self):
        self.reconcile_count += 1


@contextlib.contextmanager
def _patch_scheduler_temporal_runtime(runtime_cls=RecordingTemporalRuntime):
    runtime_cls.instances = []
    with patch(
        "pynchy.host.orchestrator.task_scheduler.TemporalSchedulerRuntime",
        new=runtime_cls,
        create=True,
    ):
        yield runtime_cls


async def _run_due_task_via_scheduler(deps, task: ScheduledTask) -> None:
    """Run the scheduled-agent runner directly under the caller's patches.

    The scheduler loop now hands due tasks to Temporal. Runner behavior still
    has focused tests here; scheduler tests separately prove the Temporal
    handoff.
    """
    import pynchy.host.orchestrator.task_scheduler as ts_mod

    if isinstance(ts_mod.get_task_run_logs, Mock):
        await ts_mod._run_scheduled_agent(task, deps)
        return

    with patch(
        "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
        new_callable=AsyncMock,
        return_value=[],
    ):
        await ts_mod._run_scheduled_agent(task, deps)


async def _run_scheduler_reconcile_once(deps) -> type[RecordingTemporalRuntime]:
    """Drive the public scheduler loop through one Temporal reconciliation poll."""
    import pynchy.host.orchestrator.task_scheduler as ts_mod

    ts_mod._scheduler_running = False

    async def stop_after_poll(delay):
        raise asyncio.CancelledError

    with (
        patch("pynchy.host.orchestrator.task_scheduler.asyncio.sleep", side_effect=stop_after_poll),
        _patch_scheduler_temporal_runtime() as runtime_cls,
    ):
        with contextlib.suppress(asyncio.CancelledError):
            await start_scheduler_loop(deps)
    return runtime_cls


class TestScheduledTaskSnapshotDict:
    """Test ScheduledTask.to_snapshot_dict() serialization.

    This method is used by both app.py and task_scheduler.py to build
    the tasks snapshot written to IPC for containers. Getting the field
    mapping wrong would break container task visibility.
    """

    def test_includes_all_required_fields(self):
        task = ScheduledTask(
            id="task-42",
            group_folder="my-group",
            chat_jid="jid@g.us",
            prompt="Do something",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            next_run="2026-02-15T09:00:00+00:00",
            status="active",
        )
        d = task.to_snapshot_dict()
        assert d == {
            "id": "task-42",
            "type": "agent",
            "groupFolder": "my-group",
            "prompt": "Do something",
            "schedule_type": "cron",
            "schedule_value": "0 9 * * *",
            "status": "active",
            "next_run": "2026-02-15T09:00:00+00:00",
        }

    def test_next_run_none(self):
        """Once tasks may have no next_run — ensure it serializes as None."""
        task = ScheduledTask(
            id="task-once",
            group_folder="g",
            chat_jid="j@g.us",
            prompt="p",
            schedule_type="once",
            schedule_value="2026-01-01T00:00:00",
            context_mode="isolated",
            next_run=None,
            status="completed",
        )
        d = task.to_snapshot_dict()
        assert d["next_run"] is None
        assert d["status"] == "completed"

    def test_uses_camel_case_group_folder(self):
        """Container expects 'groupFolder' (camelCase), not 'group_folder'."""
        task = ScheduledTask(
            id="t",
            group_folder="test-folder",
            chat_jid="j@g.us",
            prompt="p",
            schedule_type="interval",
            schedule_value="60000",
            context_mode="group",
        )
        d = task.to_snapshot_dict()
        assert "groupFolder" in d
        assert "group_folder" not in d
        assert d["groupFolder"] == "test-folder"

    def test_excludes_internal_fields(self):
        """Fields like chat_jid, context_mode, repo_access are internal
        and should not leak into the snapshot dict."""
        task = ScheduledTask(
            id="t",
            group_folder="g",
            chat_jid="secret@g.us",
            prompt="p",
            schedule_type="cron",
            schedule_value="* * * * *",
            context_mode="group",
            repo_access="owner/pynchy",
            last_run="2026-01-01",
            last_result="ok",
            created_at="2026-01-01",
        )
        d = task.to_snapshot_dict()
        assert "chat_jid" not in d
        assert "context_mode" not in d
        assert "repo_access" not in d
        assert "last_run" not in d
        assert "last_result" not in d
        assert "created_at" not in d


class MockSchedulerDeps:
    """Mock implementation of SchedulerDependencies protocol."""

    def __init__(self):
        self.groups: dict[str, WorkspaceProfile] = {}
        self.queue = GroupQueue()
        self.messages: list = []
        self.host_messages: list = []
        self.system_notices: list = []
        self.agent_runs: list = []
        self.streamed_outputs: list = []
        # Configurable return value for run_agent
        self._run_agent_result: str = "success"
        # Configurable side effect for run_agent (to call on_output)
        self._run_agent_side_effect = None

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self.groups

    async def broadcast_to_channels(self, jid: str, event) -> None:
        self.messages.append((jid, event))

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        self.host_messages.append((chat_jid, text))

    async def broadcast_system_notice(self, chat_jid: str, text: str) -> None:
        self.system_notices.append((chat_jid, text))

    async def run_agent(
        self,
        group,
        chat_jid,
        messages,
        on_output=None,
        extra_system_notices=None,
        *,
        is_scheduled_task=False,
        repo_access_override=None,
        input_source="user",
    ) -> str:
        self.agent_runs.append(
            {
                "group": group,
                "chat_jid": chat_jid,
                "messages": messages,
                "on_output": on_output,
                "is_scheduled_task": is_scheduled_task,
                "repo_access_override": repo_access_override,
                "input_source": input_source,
            }
        )
        if self._run_agent_side_effect:
            return await self._run_agent_side_effect(
                group,
                chat_jid,
                messages,
                on_output,
                is_scheduled_task=is_scheduled_task,
                repo_access_override=repo_access_override,
                input_source=input_source,
            )
        return self._run_agent_result

    async def handle_streamed_output(self, chat_jid, group, result) -> bool:
        self.streamed_outputs.append((chat_jid, group, result))
        return bool(result.result)


@pytest.fixture
def mock_deps():
    """Create mock scheduler dependencies."""
    return MockSchedulerDeps()


@pytest.fixture
def sample_task():
    """Create a sample scheduled task."""
    return ScheduledTask(
        id="task-1",
        group_folder="test-group",
        chat_jid="test@g.us",
        prompt="Test task",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        context_mode="isolated",
        next_run=datetime.now(UTC).isoformat(),
        status="active",
    )


@pytest.fixture
def sample_group():
    """Create a sample registered group."""
    return WorkspaceProfile(
        jid="test@g.us",
        name="Test Group",
        folder="test-group",
        trigger="@bot",
        added_at=datetime.now(UTC).isoformat(),
    )


@pytest.fixture(autouse=True)
def reset_scheduler_state(monkeypatch):
    """Reset the scheduler's module-level globals before each test.

    ``_scheduler_running`` lives for the whole process; a raw rebind would leak
    across tests and xdist workers. Using ``monkeypatch`` auto-reverts it,
    keeping tests order-independent.
    """
    monkeypatch.setattr("pynchy.host.orchestrator.task_scheduler._scheduler_running", False)


class TestStartSchedulerLoop:
    """Test the scheduler loop initialization and duplicate prevention."""

    def test_scheduler_config_defaults_to_temporal_connection(self):
        """Scheduler config exposes the Temporal connection without a local backend switch."""
        cfg = SchedulerConfig()

        assert not hasattr(cfg, "backend")
        assert cfg.temporal_address == "localhost:7233"
        assert cfg.temporal_namespace == "default"
        assert cfg.temporal_task_queue == "pynchy-scheduler"

    @pytest.mark.asyncio
    async def test_scheduler_reconciles_temporal_schedules_without_local_due_polling(
        self, mock_deps
    ):
        """The scheduler loop reconciles Temporal schedules instead of running due work."""
        enqueued = []
        mock_deps.queue.enqueue_task = lambda group_jid, task_id, fn: enqueued.append(task_id)

        scheduler = SchedulerConfig(
            poll_interval=0.01,
            temporal_address="localhost:7233",
            temporal_namespace="default",
            temporal_task_queue="pynchy-test",
        )

        with (
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as mock_spawn,
            _patch_scheduler_temporal_runtime() as runtime_cls,
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ),
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.get_settings",
                return_value=make_settings(scheduler=scheduler),
            ):
                with contextlib.suppress(asyncio.CancelledError):
                    await start_scheduler_loop(mock_deps)

        assert enqueued == []
        assert len(runtime_cls.instances) == 1
        assert runtime_cls.instances[0].reconcile_count == 1
        mock_spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prevents_duplicate_scheduler_start(self, mock_deps):
        """Should prevent starting multiple scheduler loops."""
        with _patch_scheduler_temporal_runtime() as runtime_cls:
            # Start first scheduler
            task1 = asyncio.create_task(start_scheduler_loop(mock_deps))
            await asyncio.sleep(0.01)  # Let it start

            # Try to start second scheduler
            task2 = asyncio.create_task(start_scheduler_loop(mock_deps))
            await asyncio.sleep(0.01)

            # Cancel both
            task1.cancel()
            task2.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task1

            with contextlib.suppress(asyncio.CancelledError):
                await task2

        assert len(runtime_cls.instances) == 1

    @pytest.mark.asyncio
    async def test_scheduler_start_can_retry_after_temporal_start_failure(self, mock_deps):
        """A failed Temporal startup should not poison later scheduler starts."""

        class FailingTemporalRuntime:
            instances = []

            def __init__(self, deps, scheduler_config):
                FailingTemporalRuntime.instances.append(self)

            async def __aenter__(self):
                raise RuntimeError("temporal unavailable")

            async def __aexit__(self, exc_type, exc, _tb):
                pass

        async def stop_on_first_poll(delay):
            raise asyncio.CancelledError

        with _patch_scheduler_temporal_runtime(FailingTemporalRuntime):
            with pytest.raises(RuntimeError, match="temporal unavailable"):
                await start_scheduler_loop(mock_deps)

        with (
            _patch_scheduler_temporal_runtime() as runtime_cls,
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep",
                side_effect=stop_on_first_poll,
            ),
            _patch_settings(poll_interval=0.01),
        ):
            with contextlib.suppress(asyncio.CancelledError):
                await start_scheduler_loop(mock_deps)

        assert len(FailingTemporalRuntime.instances) == 1
        assert len(runtime_cls.instances) == 1

    @pytest.mark.asyncio
    async def test_scheduler_loop_reconciles_temporal_schedules_repeatedly(self, mock_deps):
        """Should continuously reconcile Temporal schedules."""
        sleep_count = 0

        async def mock_sleep(delay):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with _patch_scheduler_temporal_runtime() as runtime_cls:
            with patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep", side_effect=mock_sleep
            ):
                with _patch_settings(poll_interval=0.01):
                    with contextlib.suppress(asyncio.CancelledError):
                        await start_scheduler_loop(mock_deps)

        assert runtime_cls.instances[0].reconcile_count >= 2

    @pytest.mark.asyncio
    async def test_scheduler_loop_handles_exceptions_gracefully(self, mock_deps):
        """Should catch and log exceptions without crashing."""
        error_count = 0

        async def mock_reconcile():
            nonlocal error_count
            error_count += 1
            if error_count == 1:
                raise ValueError("Test error")

        async def mock_sleep(delay):
            if error_count >= 2:
                raise asyncio.CancelledError

        with _patch_scheduler_temporal_runtime() as runtime_cls:
            with patch.object(runtime_cls, "reconcile_schedules", side_effect=mock_reconcile):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.asyncio.sleep", side_effect=mock_sleep
                ):
                    with _patch_settings(poll_interval=0.01):
                        with contextlib.suppress(asyncio.CancelledError):
                            await start_scheduler_loop(mock_deps)

                    # Should have continued after the error
                    assert error_count >= 2

    @pytest.mark.asyncio
    async def test_scheduler_does_not_enqueue_due_tasks_locally(self, mock_deps):
        """Due work is Temporal schedule state, not local GroupQueue work."""
        enqueued = []

        def track_enqueue(group_jid, task_id, fn):
            enqueued.append((group_jid, task_id))

        mock_deps.queue.enqueue_task = track_enqueue

        with _patch_scheduler_temporal_runtime() as runtime_cls:
            with patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ):
                with _patch_settings(poll_interval=0.01):
                    with contextlib.suppress(asyncio.CancelledError):
                        await start_scheduler_loop(mock_deps)

        assert enqueued == []
        assert len(runtime_cls.instances) == 1


class TestRunScheduledAgent:
    """Test task execution logic.

    Since _run_scheduled_agent now delegates to deps.run_agent (the unified
    entry point), these tests verify that the scheduler correctly constructs
    messages, passes the right flags, handles return values, and logs runs.
    """

    @pytest.mark.asyncio
    async def test_pauses_task_before_execution_after_repeated_same_failure(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Repeated identical failures trip the scheduled-task circuit breaker."""
        mock_deps.groups["test-jid"] = sample_group
        previous = [
            TaskRunLog(
                task_id=sample_task.id,
                run_at=f"2024-06-01T00:0{i}:00Z",
                duration_ms=10,
                status="error",
                result=None,
                error="API Error: 429 rate limit on request 123",
                error_signature="API Error: # rate limit on request #",
            )
            for i in range(3)
        ]
        updates = []
        logged_runs = []

        async def mock_update(task_id, update):
            updates.append((task_id, update))

        async def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.get_task_run_logs",
            new_callable=AsyncMock,
            return_value=previous,
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task", side_effect=mock_update
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.log_task_run",
                    side_effect=mock_log_run,
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert mock_deps.agent_runs == []
        assert updates == [(sample_task.id, {"status": "paused"})]
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert logged_runs[0].escalation_reason == "stagnation"
        assert "Same error repeated" in (logged_runs[0].error or "")

    @pytest.mark.asyncio
    async def test_logs_error_when_group_not_found(self, mock_deps, sample_task, tmp_path):
        """Should log error when group is not registered."""
        logged_runs = []

        async def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
            ):
                with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                    await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have logged an error
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "Group not found" in logged_runs[0].error

    @pytest.mark.asyncio
    async def test_calls_run_agent_with_correct_flags(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should call run_agent with is_scheduled_task=True and input_source='scheduled_task'."""
        mock_deps.groups["test-jid"] = sample_group

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert len(mock_deps.agent_runs) == 1
        run = mock_deps.agent_runs[0]
        assert run["is_scheduled_task"] is True
        assert run["input_source"] == "scheduled_task"
        assert run["chat_jid"] == "test@g.us"
        # Verify prompt was passed as a user message
        assert len(run["messages"]) == 1
        assert run["messages"][0]["content"] == "Test task"
        assert run["messages"][0]["sender"] == "scheduled_task"

    @pytest.mark.asyncio
    async def test_scheduled_agent_resolves_workspace_repo_config(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Scheduled agents use workspace profile repos, not stale task repo state."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.repo_access = "owner/pynchy"

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert len(mock_deps.agent_runs) == 1
        assert mock_deps.agent_runs[0]["repo_access_override"] is None

    @pytest.mark.asyncio
    async def test_sends_start_notification(self, mock_deps, sample_task, sample_group, tmp_path):
        """Should broadcast start notification before running agent."""
        mock_deps.groups["test-jid"] = sample_group

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Check that a scheduled task starting event was broadcast
        assert len(mock_deps.messages) >= 1
        jid, event = mock_deps.messages[0]
        assert jid == "test@g.us"
        assert "\u23f1 Scheduled task starting." in event.content

    @pytest.mark.asyncio
    async def test_on_output_delegates_to_handle_streamed_output(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should delegate streamed output to deps.handle_streamed_output."""
        mock_deps.groups["test-jid"] = sample_group
        streamed = ContainerOutput(status="success", result="Task completed")

        async def mock_run(group, chat_jid, messages, on_output, **kwargs):
            if on_output:
                await on_output(streamed)
            return "success"

        mock_deps._run_agent_side_effect = mock_run

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have delegated to handle_streamed_output
        assert len(mock_deps.streamed_outputs) == 1
        assert mock_deps.streamed_outputs[0][0] == "test@g.us"

    @pytest.mark.asyncio
    async def test_calculates_next_run_for_cron_schedule(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should calculate next run time for cron schedules."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "cron"
        sample_task.schedule_value = "0 9 * * *"  # Daily at 9am

        updates = []

        async def mock_update(task_id, next_run, result_summary):
            updates.append((task_id, next_run, result_summary))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                side_effect=mock_update,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have calculated next run
        assert len(updates) == 1
        assert updates[0][0] == "task-1"
        assert updates[0][1] is not None  # Should have a next run time
        # Verify it's a valid ISO timestamp
        datetime.fromisoformat(updates[0][1])

    @pytest.mark.asyncio
    async def test_calculates_next_run_for_interval_schedule(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should calculate next run time for interval schedules."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "interval"
        sample_task.schedule_value = "300000"  # 5 minutes in ms

        updates = []

        async def mock_update(task_id, next_run, result_summary):
            updates.append((task_id, next_run, result_summary))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                side_effect=mock_update,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have calculated next run
        assert len(updates) == 1
        assert updates[0][1] is not None
        # Next run should be roughly 5 minutes from now
        next_run_dt = datetime.fromisoformat(updates[0][1])
        now = datetime.now(UTC)
        diff = (next_run_dt - now).total_seconds()
        assert 290 < diff < 310  # Allow some tolerance

    @pytest.mark.asyncio
    async def test_no_next_run_for_once_schedule(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should not calculate next run for 'once' schedules."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "once"
        sample_task.schedule_value = "2024-12-31T23:59:59"

        updates = []

        async def mock_update(task_id, next_run, result_summary):
            updates.append((task_id, next_run, result_summary))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                side_effect=mock_update,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have no next run for 'once' tasks
        assert len(updates) == 1
        assert updates[0][1] is None

    @pytest.mark.asyncio
    async def test_logs_error_on_agent_exception(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should log error when run_agent raises an exception."""
        mock_deps.groups["test-jid"] = sample_group

        async def mock_run_raise(group, chat_jid, messages, on_output, **kwargs):
            raise ValueError("Agent failed")

        mock_deps._run_agent_side_effect = mock_run_raise

        logged_runs = []

        async def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have logged the error
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "Agent failed" in logged_runs[0].error

    @pytest.mark.asyncio
    async def test_logs_error_on_agent_error_return(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should log error when run_agent returns 'error'."""
        mock_deps.groups["test-jid"] = sample_group
        mock_deps._run_agent_result = "error"

        logged_runs = []

        async def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have logged the error
        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "Agent returned error" in logged_runs[0].error

    @pytest.mark.asyncio
    async def test_advances_next_run_before_execution(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should advance next_run in DB before running agent to prevent re-queuing."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.schedule_type = "cron"
        sample_task.schedule_value = "0 4 * * *"

        early_updates = []

        async def mock_update(task_id, updates):
            early_updates.append((task_id, updates))

        with patch("pynchy.host.orchestrator.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", side_effect=mock_update
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        # Should have called update_task with next_run before execution
        assert len(early_updates) == 1
        assert early_updates[0][0] == "task-1"
        assert "next_run" in early_updates[0][1]
        # next_run should be a future ISO timestamp
        next_run_dt = datetime.fromisoformat(early_updates[0][1]["next_run"])
        assert next_run_dt > datetime.now(UTC)

    @pytest.mark.asyncio
    async def test_detects_error_status_from_container(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should classify streamed status='error' outputs (e.g. SDK is_error) as task errors."""
        mock_deps.groups["test-jid"] = sample_group
        api_error_text = (
            'API Error: 429 {"error":{"type":"rate_limit_error","message":'
            '"This request would exceed your account\'s rate limit."}}'
        )
        # Container now emits status="error" when ResultMessage.is_error is True
        streamed = ContainerOutput(status="error", error=api_error_text)

        async def mock_run(group, chat_jid, messages, on_output, **kwargs):
            if on_output:
                await on_output(streamed)
            return "success"

        mock_deps._run_agent_side_effect = mock_run

        logged_runs = []

        async def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch(
            "pynchy.host.orchestrator.task_scheduler.log_task_run", side_effect=mock_log_run
        ):
            with patch(
                "pynchy.host.orchestrator.task_scheduler.update_task_after_run",
                new_callable=AsyncMock,
            ):
                with patch(
                    "pynchy.host.orchestrator.task_scheduler.update_task", new_callable=AsyncMock
                ):
                    with _patch_settings(groups_dir=tmp_path, poll_interval=0.01):
                        await _run_due_task_via_scheduler(mock_deps, sample_task)

        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "API Error: 429" in logged_runs[0].error


class TestHostCronJobs:
    @pytest.mark.asyncio
    async def test_configured_host_cron_jobs_are_not_spawned_by_scheduler(
        self, tmp_path, monkeypatch
    ):
        with (
            _patch_settings(
                cron_jobs={
                    "rebuild_container": CronJobConfig(
                        schedule="0 5 * * *",
                        command="./src/pynchy/agent/build.sh",
                    )
                },
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            runtime_cls = await _run_scheduler_reconcile_once(MockSchedulerDeps())

        mock_spawn.assert_not_awaited()
        assert runtime_cls.instances[0].reconcile_count == 1

    @pytest.mark.asyncio
    async def test_skips_disabled_host_cron_job(self, monkeypatch):
        with (
            _patch_settings(
                cron_jobs={
                    "disabled_job": CronJobConfig(
                        schedule="0 5 * * *",
                        command="echo hello",
                        enabled=False,
                    )
                },
            ),
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            runtime_cls = await _run_scheduler_reconcile_once(MockSchedulerDeps())

        mock_spawn.assert_not_awaited()
        assert runtime_cls.instances[0].reconcile_count == 1
