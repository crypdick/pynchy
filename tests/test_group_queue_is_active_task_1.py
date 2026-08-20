"""Tests for the group queue."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from conftest import make_container_runtime_operations

from pynchy.host.container_manager.ipc.write import clean_ipc_input_dir
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.turn_outcomes import TurnOutcome
from tests.group_queue_support import (
    _patch_settings,
    _queue_policy,
    _runtime,
    _target,
)

pytest_plugins = ("tests.group_queue_support",)

TASK_EXPLODED_MESSAGE = "task exploded"

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pynchy.host.orchestrator.api import ContainerRuntimeOperations


class TestIsActiveTask:
    """Tests for is_active_task: checking whether a scheduled task is active."""

    def test_stale_runtime_controls_are_noops(self, queue: GroupQueue) -> None:
        runtime_id = _runtime("missing")

        queue.defer_interrupt_until_tool_result(runtime_id)

    def test_returns_false_when_group_not_active(self, queue: GroupQueue):
        """Not active when no container is running for the group."""
        assert queue.is_active_task(_runtime("group1@g.us")) is False
        assert queue.has_active_run(_runtime("group1@g.us")) is False

    async def test_returns_true_when_task_is_active(self, queue: GroupQueue):
        """Returns True when the active container is a scheduled task."""
        completions: list[asyncio.Event] = []

        async def task_fn():
            event = asyncio.Event()
            completions.append(event)
            await event.wait()

        queue.enqueue_task(_target("group1@g.us", "test-group"), "task-1", task_fn)
        await asyncio.sleep(0.02)

        assert queue.is_active_task(_runtime("test-group")) is True

        completions[0].set()
        await asyncio.sleep(0.05)

    async def test_returns_false_when_message_is_active(self, queue: GroupQueue):
        """Returns False when the active container is processing messages (not a task)."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check(_target("group1@g.us"))
        await asyncio.sleep(0.02)

        assert queue.is_active_task(_runtime("group1@g.us")) is False

        completions[0].set()
        await asyncio.sleep(0.05)

    async def test_returns_false_after_task_completes(self, queue: GroupQueue):
        """is_active_task returns False after a task completes."""

        def task_fn() -> Awaitable[None]:
            return asyncio.sleep(0)

        queue.enqueue_task(_target("group1@g.us", "test-group"), "task-1", task_fn)
        await asyncio.sleep(0.1)

        assert queue.is_active_task(_runtime("test-group")) is False
        await queue.stop_active_process(_runtime("test-group"))


class TestTaskExceptionHandling:
    """Tests for error handling during task execution."""

    async def test_task_exception_does_not_crash_queue(self, queue: GroupQueue):
        """An exception in a task should be caught and not crash the queue."""

        def failing_task() -> Awaitable[None]:
            raise RuntimeError(TASK_EXPLODED_MESSAGE)

        queue.enqueue_task(_target("group1@g.us"), "task-crash", failing_task)
        await asyncio.sleep(0.1)

        # Queue should still be functional
        snapshot = queue.snapshot()
        assert snapshot["_meta"]["active_count"] == 0
        assert snapshot["group1@g.us"]["active"] is False

    async def test_exception_in_process_messages_schedules_retry(self):
        """When process_messages raises, the queue schedules a retry."""
        call_count = 0

        def process_messages(group_jid: str) -> Awaitable[TurnOutcome]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            return asyncio.sleep(0, result=TurnOutcome.COMPLETED)

        with _patch_settings(max_concurrent=2, base_retry_seconds=0.05):
            queue = GroupQueue(
                _queue_policy(base_retry_seconds=0.05), make_container_runtime_operations()
            )
            queue.set_process_messages_fn(process_messages)
            queue.enqueue_message_check(_target("group1@g.us"))

            await asyncio.sleep(0.02)
            assert call_count == 1
            # State should be cleaned up after exception
            assert queue.snapshot()["group1@g.us"]["active"] is False

            # Retry should fire
            await asyncio.sleep(0.15)
            assert call_count >= 2


class TestStopActiveProcess:
    """Tests for stop_active_process: force-stopping active containers."""

    async def test_noop_when_not_active(self, queue: GroupQueue):
        """stop_active_process does nothing when group is not active."""
        await queue.stop_active_process(_runtime("group1@g.us"))
        await queue.stop_active_process_for_control(_runtime("group1@g.us"))

    async def test_calls_graceful_stop_on_active_process(
        self, queue: GroupQueue, container_runtime: ContainerRuntimeOperations
    ):
        """stop_active_process writes _close sentinel and calls graceful_stop."""
        completions: list[asyncio.Event] = []

        async def task_fn():
            event = asyncio.Event()
            completions.append(event)
            await event.wait()

        queue.enqueue_task(_target("group1@g.us", "test-group"), "task-1", task_fn)
        await asyncio.sleep(0.02)

        # Register a mock process
        mock_proc = AsyncMock(spec=asyncio.subprocess.Process)
        mock_proc.returncode = None
        queue.register_process(_runtime("test-group"), mock_proc, "my-container")

        assert queue.has_active_run(_runtime("test-group")) is True
        container_runtime.write_message.side_effect = OSError("IPC unavailable")
        assert queue.send_message(_runtime("test-group"), "follow-up") is False
        container_runtime.write_close_sentinel.side_effect = OSError("IPC unavailable")
        queue.close_stdin(_runtime("test-group"))
        container_runtime.write_close_sentinel.reset_mock()

        await queue.stop_active_process(_runtime("test-group"))

        container_runtime.write_close_sentinel.assert_called_once_with("test-group")
        container_runtime.graceful_stop.assert_awaited_once_with(mock_proc, "my-container")

        completions[0].set()
        await asyncio.sleep(0.05)


class TestDeferredToolBoundaryInterrupt:
    def test_host_runner_rejects_ipc_messages_for_boundary_delivery(self, queue: GroupQueue):
        """Host execution has no IPC watcher and must leave input pending."""
        lease = queue.acquire_host_process(_target("group1@g.us", "pynchy-dev"))
        assert queue.register_host_process(lease, None, "host-agent-runner")

        assert queue.send_message(_runtime("pynchy-dev"), "follow-up") is False
        assert queue.release_host_process(lease) is False

    async def test_stops_only_when_a_deferred_interrupt_is_requested(self, queue: GroupQueue):
        lease = queue.acquire_host_process(_target("group1@g.us"))

        assert await queue.interrupt_after_tool_result(_runtime("group1@g.us")) is False

        queue.defer_interrupt_until_tool_result(_runtime("group1@g.us"))
        with patch.object(queue, "stop_active_process", new_callable=AsyncMock) as stop:
            assert await queue.interrupt_after_tool_result(_runtime("group1@g.us")) is True

        stop.assert_awaited_once_with(_runtime("group1@g.us"))
        assert queue.release_host_process(lease) is True

    async def test_stops_host_runner_without_container_cleanup(self, queue: GroupQueue):
        lease = queue.acquire_host_process(_target("group1@g.us", "pynchy-dev"))
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        assert queue.register_host_process(
            lease,
            proc,
            "host-agent-runner",
        )

        with patch(
            "pynchy.host.orchestrator.runtime_process_control.stop_host_process",
            new_callable=AsyncMock,
        ) as stop:
            await queue.stop_active_process(_runtime("pynchy-dev"))

        stop.assert_awaited_once_with(proc)
        assert queue.release_host_process(lease) is False

    async def test_skips_graceful_stop_when_already_exited(
        self, queue: GroupQueue, container_runtime: ContainerRuntimeOperations
    ):
        """stop_active_process skips graceful_stop if process already exited."""
        completions: list[asyncio.Event] = []

        async def task_fn():
            event = asyncio.Event()
            completions.append(event)
            await event.wait()

        queue.enqueue_task(_target("group1@g.us", "test-group"), "task-1", task_fn)
        await asyncio.sleep(0.02)

        mock_proc = AsyncMock(spec=asyncio.subprocess.Process)
        mock_proc.returncode = 0  # already exited
        queue.register_process(_runtime("test-group"), mock_proc, "my-container")

        await queue.stop_active_process(_runtime("test-group"))
        container_runtime.graceful_stop.assert_not_awaited()

        completions[0].set()
        await asyncio.sleep(0.05)


class TestClearPendingTasks:
    """Tests for clear_pending_tasks: dropping queued tasks."""

    async def test_clears_pending_tasks(self, queue: GroupQueue):
        """clear_pending_tasks removes all pending tasks for a group."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check(_target("group1@g.us"))
        await asyncio.sleep(0.02)

        async def task_fn():
            pass

        queue.enqueue_task(_target("group1@g.us"), "task-1", task_fn)
        queue.enqueue_task(_target("group1@g.us"), "task-2", task_fn)

        assert queue.snapshot()["group1@g.us"]["pending_tasks"] == 2

        cleared = queue.clear_pending_tasks(_runtime("group1@g.us"))
        assert queue.snapshot()["group1@g.us"]["pending_tasks"] == 0
        assert cleared == ("task-1", "task-2")

        completions[0].set()
        await asyncio.sleep(0.05)

    def test_noop_when_no_pending_tasks(self, queue: GroupQueue):
        """clear_pending_tasks does nothing when there are no pending tasks."""
        assert queue.clear_pending_tasks(_runtime("group1@g.us")) == ()
        assert "group1@g.us" not in queue.snapshot()


class TestDrainGroupTaskOrdering:
    """Tests for _drain_group prioritization."""

    async def test_tasks_queued_at_concurrency_limit_drain_correctly(self):
        """Tasks queued when at concurrency limit drain when slots free up."""
        with _patch_settings(max_concurrent=1):
            queue = GroupQueue(_queue_policy(max_concurrent=1), make_container_runtime_operations())
            execution: list[str] = []
            completions: list[asyncio.Event] = []

            async def process_messages(group_jid: str) -> TurnOutcome:
                execution.append(f"msg-{group_jid}")
                event = asyncio.Event()
                completions.append(event)
                await event.wait()
                return TurnOutcome.COMPLETED

            queue.set_process_messages_fn(process_messages)

            # Fill the single slot
            queue.enqueue_message_check(_target("group1@g.us"))
            await asyncio.sleep(0.02)

            # Queue a task for a different group while at limit
            def task_fn() -> Awaitable[None]:
                execution.append("task-group2")
                return asyncio.sleep(0)

            queue.enqueue_task(_target("group2@g.us"), "task-1", task_fn)

            # Free the slot
            completions[0].set()
            await asyncio.sleep(0.1)

            # Task should have been drained
            assert "task-group2" in execution


class TestCleanupIpcInput:
    """Tests for clean_ipc_input_dir: stale IPC file removal after task exit.

    clean_ipc_input_dir lives in ipc._write but is tested here alongside
    the queue behaviour that calls it.
    """

    def test_removes_all_files(self, tmp_path):
        """Should remove all files (json, sentinel, etc.) from the IPC input dir."""
        input_dir = tmp_path / "ipc" / "test-group" / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "001.json").write_text('{"type": "message", "text": "stale"}')
        (input_dir / "002.json").write_text('{"type": "message", "text": "also stale"}')
        (input_dir / "_close").write_text("")

        with _patch_settings(max_concurrent=2, data_dir=tmp_path):
            clean_ipc_input_dir("test-group")

        assert list(input_dir.iterdir()) == []

    def test_noop_when_no_group_folder(self):
        """Should silently do nothing when group_folder is None."""
        clean_ipc_input_dir(None)  # No error raised

    def test_noop_when_dir_doesnt_exist(self, tmp_path):
        """Should silently do nothing when IPC input dir doesn't exist."""
        with _patch_settings(max_concurrent=2, data_dir=tmp_path):
            clean_ipc_input_dir("nonexistent-group")
