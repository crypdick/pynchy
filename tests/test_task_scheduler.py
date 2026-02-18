"""Tests for task scheduler.

Tests the scheduled task execution logic, including:
- Scheduler loop initialization and duplicate prevention
- Task polling and due task detection
- Task execution with different context modes
- Next run calculation for cron, interval, and once schedules
- Error handling and logging
- Group lookup and validation
"""

# ruff: noqa: SIM117, E501

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.config import CronJobConfig, SchedulerConfig
from pynchy.group_queue import GroupQueue
from pynchy.task_scheduler import start_scheduler_loop
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
    with patch("pynchy.task_scheduler.get_settings", return_value=s):
        yield


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
        """Fields like chat_jid, context_mode, pynchy_repo_access are internal
        and should not leak into the snapshot dict."""
        task = ScheduledTask(
            id="t",
            group_folder="g",
            chat_jid="secret@g.us",
            prompt="p",
            schedule_type="cron",
            schedule_value="* * * * *",
            context_mode="group",
            pynchy_repo_access=True,
            last_run="2026-01-01",
            last_result="ok",
            created_at="2026-01-01",
        )
        d = task.to_snapshot_dict()
        assert "chat_jid" not in d
        assert "context_mode" not in d
        assert "pynchy_repo_access" not in d
        assert "last_run" not in d
        assert "last_result" not in d
        assert "created_at" not in d


class MockSchedulerDeps:
    """Mock implementation of SchedulerDependencies protocol."""

    def __init__(self):
        self.groups: dict[str, WorkspaceProfile] = {}
        self.queue = GroupQueue()
        self.messages: list = []
        self.agent_runs: list = []
        self.streamed_outputs: list = []
        # Configurable return value for run_agent
        self._run_agent_result: str = "success"
        # Configurable side effect for run_agent (to call on_output)
        self._run_agent_side_effect = None

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self.groups

    async def broadcast_to_channels(self, jid: str, text: str) -> None:
        self.messages.append((jid, text))

    async def run_agent(
        self,
        group,
        chat_jid,
        messages,
        on_output=None,
        extra_system_notices=None,
        *,
        is_scheduled_task=False,
        pynchy_repo_access_override=None,
        input_source="user",
    ) -> str:
        self.agent_runs.append(
            {
                "group": group,
                "chat_jid": chat_jid,
                "messages": messages,
                "on_output": on_output,
                "is_scheduled_task": is_scheduled_task,
                "pynchy_repo_access_override": pynchy_repo_access_override,
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
                pynchy_repo_access_override=pynchy_repo_access_override,
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


class TestStartSchedulerLoop:
    """Test the scheduler loop initialization and duplicate prevention."""

    def setup_method(self):
        """Reset scheduler state before each test."""
        import pynchy.task_scheduler

        pynchy.task_scheduler._scheduler_running = False
        pynchy.task_scheduler._cron_job_next_runs = {}

    @pytest.mark.asyncio
    async def test_prevents_duplicate_scheduler_start(self, mock_deps):
        """Should prevent starting multiple scheduler loops."""
        with patch("pynchy.task_scheduler.get_due_tasks", new_callable=AsyncMock) as mock_get_due:
            mock_get_due.return_value = []

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

            # Second call should have returned immediately without polling
            # We can't easily test the internal state, but at least it doesn't crash

    @pytest.mark.asyncio
    async def test_scheduler_loop_polls_for_due_tasks(self, mock_deps):
        """Should continuously poll for due tasks."""
        poll_count = 0

        async def mock_get_due():
            nonlocal poll_count
            poll_count += 1
            if poll_count >= 2:
                # Stop after 2 polls
                raise asyncio.CancelledError()
            return []

        with patch("pynchy.task_scheduler.get_due_tasks", side_effect=mock_get_due):
            with _patch_settings(poll_interval=0.01):
                with contextlib.suppress(asyncio.CancelledError):
                    await start_scheduler_loop(mock_deps)

                assert poll_count >= 2

    @pytest.mark.asyncio
    async def test_scheduler_loop_handles_exceptions_gracefully(self, mock_deps):
        """Should catch and log exceptions without crashing."""
        error_count = 0

        async def mock_get_due():
            nonlocal error_count
            error_count += 1
            if error_count == 1:
                raise ValueError("Test error")
            elif error_count >= 2:
                raise asyncio.CancelledError()
            return []

        with patch("pynchy.task_scheduler.get_due_tasks", side_effect=mock_get_due):
            with _patch_settings(poll_interval=0.01):
                with contextlib.suppress(asyncio.CancelledError):
                    await start_scheduler_loop(mock_deps)

                # Should have continued after the error
                assert error_count >= 2

    @pytest.mark.asyncio
    async def test_enqueues_due_tasks_to_group_queue(self, mock_deps, sample_task):
        """Should enqueue due tasks to the group queue."""
        enqueued = []

        original_enqueue = mock_deps.queue.enqueue_task

        def track_enqueue(group_jid, task_id, fn):
            enqueued.append((group_jid, task_id))
            return original_enqueue(group_jid, task_id, fn)

        mock_deps.queue.enqueue_task = track_enqueue

        poll_count = [0]

        async def mock_get_due():
            poll_count[0] += 1
            if poll_count[0] == 1:
                return [sample_task]
            raise asyncio.CancelledError()

        async def mock_get_task(task_id):
            return sample_task

        with patch("pynchy.task_scheduler.get_due_tasks", side_effect=mock_get_due):
            with patch("pynchy.task_scheduler.get_task_by_id", side_effect=mock_get_task):
                with _patch_settings(poll_interval=0.01):
                    with contextlib.suppress(asyncio.CancelledError):
                        await start_scheduler_loop(mock_deps)

        # Should have enqueued the task
        assert len(enqueued) == 1
        assert enqueued[0][0] == sample_task.chat_jid
        assert enqueued[0][1] == sample_task.id

    @pytest.mark.asyncio
    async def test_skips_paused_tasks(self, mock_deps, sample_task):
        """Should skip tasks that have been paused."""
        sample_task.status = "active"
        paused_task = ScheduledTask(
            id="task-1",
            group_folder="test-group",
            chat_jid="test@g.us",
            prompt="Test task",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            status="paused",  # Paused!
        )

        enqueued = []

        original_enqueue = mock_deps.queue.enqueue_task

        def track_enqueue(group_jid, task_id, fn):
            enqueued.append((group_jid, task_id))
            return original_enqueue(group_jid, task_id, fn)

        mock_deps.queue.enqueue_task = track_enqueue

        poll_count = [0]

        async def mock_get_due():
            poll_count[0] += 1
            if poll_count[0] == 1:
                return [sample_task]
            raise asyncio.CancelledError()

        async def mock_get_task(task_id):
            # Return paused version on re-check
            return paused_task

        with patch("pynchy.task_scheduler.get_due_tasks", side_effect=mock_get_due):
            with patch("pynchy.task_scheduler.get_task_by_id", side_effect=mock_get_task):
                with _patch_settings(poll_interval=0.01):
                    with contextlib.suppress(asyncio.CancelledError):
                        await start_scheduler_loop(mock_deps)

        # Should NOT have enqueued the paused task
        assert len(enqueued) == 0


class TestRunScheduledAgent:
    """Test task execution logic.

    Since _run_scheduled_agent now delegates to deps.run_agent (the unified
    entry point), these tests verify that the scheduler correctly constructs
    messages, passes the right flags, handles return values, and logs runs.
    """

    @pytest.mark.asyncio
    async def test_logs_error_when_group_not_found(self, mock_deps, sample_task, tmp_path):
        """Should log error when group is not registered."""
        logged_runs = []

        async def mock_log_run(log: TaskRunLog):
            logged_runs.append(log)

        with patch("pynchy.task_scheduler.log_task_run", side_effect=mock_log_run):
            with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                with _patch_settings(groups_dir=tmp_path):
                    from pynchy.task_scheduler import _run_scheduled_agent

                    await _run_scheduled_agent(sample_task, mock_deps)

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

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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
    async def test_passes_pynchy_repo_access_override(
        self, mock_deps, sample_task, sample_group, tmp_path
    ):
        """Should pass pynchy_repo_access from task as pynchy_repo_access_override."""
        mock_deps.groups["test-jid"] = sample_group
        sample_task.pynchy_repo_access = True

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

        assert len(mock_deps.agent_runs) == 1
        assert mock_deps.agent_runs[0]["pynchy_repo_access_override"] is True

    @pytest.mark.asyncio
    async def test_sends_start_notification(self, mock_deps, sample_task, sample_group, tmp_path):
        """Should broadcast start notification before running agent."""
        mock_deps.groups["test-jid"] = sample_group

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

        assert ("test@g.us", "\u23f1 Scheduled task starting.") in mock_deps.messages

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

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", side_effect=mock_update):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", side_effect=mock_update):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", side_effect=mock_update):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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

        with patch("pynchy.task_scheduler.log_task_run", side_effect=mock_log_run):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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

        with patch("pynchy.task_scheduler.log_task_run", side_effect=mock_log_run):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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

        with patch("pynchy.task_scheduler.log_task_run", new_callable=AsyncMock):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", side_effect=mock_update):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

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
        api_error_text = 'API Error: 429 {"error":{"type":"rate_limit_error","message":"This request would exceed your account\'s rate limit."}}'
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

        with patch("pynchy.task_scheduler.log_task_run", side_effect=mock_log_run):
            with patch("pynchy.task_scheduler.update_task_after_run", new_callable=AsyncMock):
                with patch("pynchy.task_scheduler.update_task", new_callable=AsyncMock):
                    with _patch_settings(groups_dir=tmp_path):
                        from pynchy.task_scheduler import _run_scheduled_agent

                        await _run_scheduled_agent(sample_task, mock_deps)

        assert len(logged_runs) == 1
        assert logged_runs[0].status == "error"
        assert "API Error: 429" in logged_runs[0].error


class TestHostCronJobs:
    @pytest.mark.asyncio
    async def test_runs_due_host_cron_job(self, tmp_path):
        import pynchy.task_scheduler

        pynchy.task_scheduler._cron_job_next_runs = {
            "rebuild_container": datetime.now(UTC).replace(microsecond=0).isoformat()
        }

        class FakeProcess:
            returncode = 0

            async def communicate(self):
                return b"build ok", b""

        fake_proc = FakeProcess()

        with (
            _patch_settings(
                cron_jobs={
                    "rebuild_container": CronJobConfig(
                        schedule="0 5 * * *",
                        command="./container/build.sh",
                    )
                },
            ),
            patch(
                "pynchy.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
                return_value=fake_proc,
            ) as mock_spawn,
        ):
            from pynchy.task_scheduler import _poll_host_cron_jobs

            await _poll_host_cron_jobs()

        mock_spawn.assert_awaited_once()
        args = mock_spawn.await_args
        assert args.args[0] == "./container/build.sh"

    @pytest.mark.asyncio
    async def test_skips_disabled_host_cron_job(self):
        import pynchy.task_scheduler

        pynchy.task_scheduler._cron_job_next_runs = {
            "disabled_job": datetime.now(UTC).replace(microsecond=0).isoformat()
        }

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
                "pynchy.task_scheduler.asyncio.create_subprocess_shell",
                new_callable=AsyncMock,
            ) as mock_spawn,
        ):
            from pynchy.task_scheduler import _poll_host_cron_jobs

            await _poll_host_cron_jobs()

        mock_spawn.assert_not_awaited()
