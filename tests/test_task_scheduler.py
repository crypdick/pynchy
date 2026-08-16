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
from contextvars import ContextVar
from unittest.mock import AsyncMock, patch

import pytest
from conftest import (
    make_settings,
)

from pynchy.config.api import SchedulerConfig
from pynchy.host.orchestrator.task_scheduler import start_scheduler_loop
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from tests.task_scheduler_support import (
    RecordingTemporalRuntime,
    _patch_scheduler_temporal_runtime,
    _patch_settings,
    _scheduler_runtime_from_settings,
)

pytest_plugins = ("tests.task_scheduler_support",)

TEMPORAL_UNAVAILABLE_MESSAGE = "temporal unavailable"
TEST_ERROR_MESSAGE = "Test error"
AGENT_FAILED_MESSAGE = "Agent failed"

_scheduler_settings: ContextVar[object | None] = ContextVar("scheduler_settings", default=None)


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
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
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
            "next_run": None,
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
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
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
            session_policy=SessionPolicy.CONTINUE,
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
            session_policy=SessionPolicy.CONTINUE,
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


class TestStartSchedulerLoop:
    """Test the scheduler loop initialization and duplicate prevention."""

    def test_scheduler_config_defaults_to_temporal_connection(self):
        """Scheduler config exposes the default Temporal connection."""
        cfg = SchedulerConfig()

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
        mock_deps._scheduler_runtime = _scheduler_runtime_from_settings(
            make_settings(scheduler=scheduler)
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
            with contextlib.suppress(asyncio.CancelledError):
                await start_scheduler_loop(mock_deps)

        assert enqueued == []
        assert len(runtime_cls.instances) == 1
        assert runtime_cls.instances[0].reconcile_count == 1
        mock_spawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prevents_duplicate_scheduler_start(self, mock_deps):
        """A second scheduler owner is rejected instead of becoming an observer."""
        with _patch_scheduler_temporal_runtime() as runtime_cls:
            owner = asyncio.create_task(start_scheduler_loop(mock_deps))
            await asyncio.sleep(0.01)

            with pytest.raises(RuntimeError, match="already running"):
                await start_scheduler_loop(mock_deps)

            owner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owner
        assert len(runtime_cls.instances) == 1

    @pytest.mark.asyncio
    async def test_shadow_runtime_owns_worker_without_reconciling_schedules(self, mock_deps):
        mock_deps._scheduler_runtime = _scheduler_runtime_from_settings(
            make_settings(scheduler=SchedulerConfig(reconcile_schedules=False))
        )
        with (
            _patch_scheduler_temporal_runtime() as runtime_cls,
            patch(
                "pynchy.host.orchestrator.task_scheduler.asyncio.sleep",
                side_effect=asyncio.CancelledError,
            ),
            contextlib.suppress(asyncio.CancelledError),
        ):
            await start_scheduler_loop(mock_deps)

        assert len(runtime_cls.instances) == 1
        assert runtime_cls.instances[0].reconcile_count == 0

    @pytest.mark.asyncio
    async def test_startup_readiness_waits_for_temporal_runtime_entry(self, mock_deps):
        """Readiness means the Temporal runtime entered, not that startup was attempted."""
        allow_entry = asyncio.Event()

        class BlockingTemporalRuntime(RecordingTemporalRuntime):
            async def __aenter__(self):
                await allow_entry.wait()
                return self

        ready = asyncio.get_running_loop().create_future()
        with _patch_scheduler_temporal_runtime(BlockingTemporalRuntime):
            owner = asyncio.create_task(start_scheduler_loop(mock_deps, ready=ready))
            await asyncio.sleep(0)
            assert not ready.done()

            allow_entry.set()
            await ready
            owner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await owner

    @pytest.mark.asyncio
    async def test_readiness_receives_temporal_start_failure(self, mock_deps):
        """The lifecycle readiness future receives the actual startup failure."""
        allow_failure = asyncio.Event()

        class FailingTemporalRuntime:
            def __init__(self, deps, scheduler_config):
                del deps, scheduler_config

            async def __aenter__(self):
                await allow_failure.wait()
                raise RuntimeError(TEMPORAL_UNAVAILABLE_MESSAGE)

            async def __aexit__(self, exc_type, exc, _tb):
                del exc_type, exc, _tb

        ready = asyncio.get_running_loop().create_future()
        with _patch_scheduler_temporal_runtime(FailingTemporalRuntime):
            owner = asyncio.create_task(start_scheduler_loop(mock_deps, ready=ready))
            await asyncio.sleep(0)
            assert not ready.done()

            allow_failure.set()
            with pytest.raises(RuntimeError, match=TEMPORAL_UNAVAILABLE_MESSAGE):
                await ready
            with pytest.raises(RuntimeError, match=TEMPORAL_UNAVAILABLE_MESSAGE):
                await owner

    @pytest.mark.asyncio
    async def test_scheduler_start_can_retry_after_temporal_start_failure(self, mock_deps):
        """A failed Temporal startup should not poison later scheduler starts."""

        class FailingTemporalRuntime:
            instances = []

            def __init__(self, deps, scheduler_config):
                FailingTemporalRuntime.instances.append(self)

            async def __aenter__(self):
                raise RuntimeError(TEMPORAL_UNAVAILABLE_MESSAGE)

            async def __aexit__(self, exc_type, exc, _tb):
                pass

        def stop_on_first_poll(delay):
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

        def mock_sleep(delay):
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

        def mock_reconcile():
            nonlocal error_count
            error_count += 1
            if error_count == 1:
                raise ValueError(TEST_ERROR_MESSAGE)

        def mock_sleep(delay):
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
