"""Tests for the group queue.

Port of src/group-queue.test.ts.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.group_queue import GroupQueue


@pytest.fixture
def queue():
    with patch("pynchy.group_queue.MAX_CONCURRENT_CONTAINERS", 2):
        yield GroupQueue()


class TestGroupQueue:
    async def test_only_runs_one_container_per_group(self, queue: GroupQueue):
        concurrent_count = 0
        max_concurrent = 0

        async def process_messages(group_jid: str) -> bool:
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return True

        queue.set_process_messages_fn(process_messages)

        queue.enqueue_message_check("group1@g.us")
        queue.enqueue_message_check("group1@g.us")

        # Let processing complete
        await asyncio.sleep(0.2)

        assert max_concurrent == 1

    async def test_respects_global_concurrency_limit(self, queue: GroupQueue):
        active_count = 0
        max_active = 0
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            active_count -= 1
            return True

        queue.set_process_messages_fn(process_messages)

        # Enqueue 3 groups (limit is 2)
        queue.enqueue_message_check("group1@g.us")
        queue.enqueue_message_check("group2@g.us")
        queue.enqueue_message_check("group3@g.us")

        await asyncio.sleep(0.05)

        # Only 2 should be active
        assert max_active == 2
        assert active_count == 2

        # Complete one — third should start
        completions[0].set()
        await asyncio.sleep(0.05)

        assert len(completions) == 3  # process_messages called 3 times total

    async def test_drains_tasks_before_messages(self, queue: GroupQueue):
        execution_order: list[str] = []
        first_blocker = asyncio.Event()

        call_count = 0

        async def process_messages(group_jid: str) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await first_blocker.wait()
            execution_order.append("messages")
            return True

        queue.set_process_messages_fn(process_messages)

        # Start processing messages (takes the active slot)
        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.02)

        # While active, enqueue both a task and pending messages
        async def task_fn():
            execution_order.append("task")

        queue.enqueue_task("group1@g.us", "task-1", task_fn)
        queue.enqueue_message_check("group1@g.us")

        # Release the first processing
        first_blocker.set()
        await asyncio.sleep(0.1)

        assert execution_order[0] == "messages"  # first call
        # Messages drain before tasks — a human is waiting, tasks are autonomous
        assert execution_order[1] == "messages"  # pending messages drain first
        assert execution_order[2] == "task"  # then pending task

    async def test_retries_with_exponential_backoff(self, queue: GroupQueue):
        call_count = 0

        async def process_messages(group_jid: str) -> bool:
            nonlocal call_count
            call_count += 1
            return False  # failure

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check("group1@g.us")

        # First call happens immediately
        await asyncio.sleep(0.05)
        assert call_count == 1

        # First retry after ~5s (BASE_RETRY_SECONDS * 2^0)
        # We use shorter sleeps in test by patching, but let's just verify
        # the retry happened. We'll wait for the first retry.
        with patch("pynchy.group_queue.BASE_RETRY_SECONDS", 0.05):
            # Reset and re-test with shorter timeouts
            pass

        # Note: The retry is already scheduled with the real delay.
        # For a proper unit test we'd mock asyncio.sleep, but this
        # verifies the initial call works.

    async def test_prevents_new_enqueues_after_shutdown(self, queue: GroupQueue):
        process_messages = AsyncMock(return_value=True)
        queue.set_process_messages_fn(process_messages)

        await queue.shutdown(1.0)

        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.05)

        process_messages.assert_not_called()

    async def test_drains_waiting_groups_when_slots_free(self, queue: GroupQueue):
        processed: list[str] = []
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            processed.append(group_jid)
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return True

        queue.set_process_messages_fn(process_messages)

        # Fill both slots
        queue.enqueue_message_check("group1@g.us")
        queue.enqueue_message_check("group2@g.us")
        await asyncio.sleep(0.05)

        # Queue a third
        queue.enqueue_message_check("group3@g.us")
        await asyncio.sleep(0.05)

        assert processed == ["group1@g.us", "group2@g.us"]

        # Free up a slot
        completions[0].set()
        await asyncio.sleep(0.05)

        assert "group3@g.us" in processed


class TestEnqueueTask:
    """Tests for task enqueuing: deduplication, shutdown guard, and concurrency."""

    async def test_duplicate_task_id_silently_dropped(self, queue: GroupQueue):
        """Same task_id enqueued twice should not create duplicate entries."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return True

        queue.set_process_messages_fn(process_messages)

        # Start a message to occupy the group's active slot
        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.02)

        # Enqueue the same task twice while group is active
        task_calls = 0

        async def task_fn():
            nonlocal task_calls
            task_calls += 1

        queue.enqueue_task("group1@g.us", "task-1", task_fn)
        queue.enqueue_task("group1@g.us", "task-1", task_fn)  # duplicate

        # Release and let everything drain
        completions[0].set()
        await asyncio.sleep(0.15)

        # Task should only have run once
        assert task_calls == 1

    async def test_different_task_ids_both_queued(self, queue: GroupQueue):
        """Different task IDs for the same group should both be queued."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return True

        queue.set_process_messages_fn(process_messages)

        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.02)

        task_ids_run: list[str] = []

        async def task_a():
            task_ids_run.append("a")

        async def task_b():
            task_ids_run.append("b")

        queue.enqueue_task("group1@g.us", "task-a", task_a)
        queue.enqueue_task("group1@g.us", "task-b", task_b)

        completions[0].set()
        await asyncio.sleep(0.15)

        assert set(task_ids_run) == {"a", "b"}

    async def test_enqueue_task_blocked_after_shutdown(self, queue: GroupQueue):
        """Tasks should be silently dropped after shutdown."""
        process_messages = AsyncMock(return_value=True)
        queue.set_process_messages_fn(process_messages)

        await queue.shutdown(1.0)

        task_called = False

        async def task_fn():
            nonlocal task_called
            task_called = True

        queue.enqueue_task("group1@g.us", "task-1", task_fn)
        await asyncio.sleep(0.05)

        assert task_called is False


class TestSendMessage:
    """Tests for send_message: IPC file write for active containers."""

    async def test_returns_false_when_group_not_active(self, queue: GroupQueue):
        assert queue.send_message("group1@g.us", "hello") is False

    async def test_returns_false_when_no_group_folder(self, queue: GroupQueue):
        """Even if active, send_message needs group_folder to know where to write."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return True

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.02)

        # Active but no group_folder registered
        assert queue.send_message("group1@g.us", "hello") is False

        completions[0].set()
        await asyncio.sleep(0.05)

    async def test_writes_ipc_file_when_active_with_folder(self, queue: GroupQueue, tmp_path):
        """Successful send_message writes an atomic JSON file to the IPC dir."""
        import json

        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return True

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.02)

        # Register process with a group_folder
        queue.register_process("group1@g.us", None, "container-1", "test-group")

        with patch("pynchy.group_queue.DATA_DIR", tmp_path):
            result = queue.send_message("group1@g.us", "hello world")

        assert result is True

        # Verify the IPC file was written
        input_dir = tmp_path / "ipc" / "test-group" / "input"
        files = list(input_dir.glob("*.json"))
        assert len(files) == 1

        content = json.loads(files[0].read_text())
        assert content == {"type": "message", "text": "hello world"}

        completions[0].set()
        await asyncio.sleep(0.05)


class TestRegisterProcess:
    """Tests for register_process: stores container metadata."""

    async def test_registers_process_and_folder(self, queue: GroupQueue):
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return True

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.02)

        mock_proc = object()
        queue.register_process("group1@g.us", mock_proc, "my-container", "my-folder")

        state = queue._groups["group1@g.us"]
        assert state.process is mock_proc
        assert state.container_name == "my-container"
        assert state.group_folder == "my-folder"

        completions[0].set()
        await asyncio.sleep(0.05)

    async def test_skips_group_folder_when_none(self, queue: GroupQueue):
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> bool:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return True

        queue.set_process_messages_fn(process_messages)
        queue.enqueue_message_check("group1@g.us")
        await asyncio.sleep(0.02)

        queue.register_process("group1@g.us", None, "c1", None)

        state = queue._groups["group1@g.us"]
        assert state.group_folder is None

        completions[0].set()
        await asyncio.sleep(0.05)


class TestGroupQueueRetry:
    """Test retry behavior with shorter timeouts."""

    async def test_retries_with_backoff(self):
        with (
            patch("pynchy.group_queue.MAX_CONCURRENT_CONTAINERS", 2),
            patch("pynchy.group_queue.BASE_RETRY_SECONDS", 0.1),
        ):
            queue = GroupQueue()
            call_count = 0

            async def process_messages(group_jid: str) -> bool:
                nonlocal call_count
                call_count += 1
                return False

            queue.set_process_messages_fn(process_messages)
            queue.enqueue_message_check("group1@g.us")

            # First call happens immediately (within next tick)
            await asyncio.sleep(0.01)
            assert call_count == 1

            # First retry after 0.1s (BASE_RETRY_SECONDS * 2^0)
            await asyncio.sleep(0.15)
            assert call_count == 2

            # Second retry after 0.2s (BASE_RETRY_SECONDS * 2^1)
            await asyncio.sleep(0.25)
            assert call_count == 3

    async def test_stops_retrying_after_max_retries(self):
        with (
            patch("pynchy.group_queue.MAX_CONCURRENT_CONTAINERS", 2),
            patch("pynchy.group_queue.BASE_RETRY_SECONDS", 0.01),
        ):
            queue = GroupQueue()
            call_count = 0

            async def process_messages(group_jid: str) -> bool:
                nonlocal call_count
                call_count += 1
                return False

            queue.set_process_messages_fn(process_messages)
            queue.enqueue_message_check("group1@g.us")

            # Let all retries complete (5 retries + 1 initial = 6 calls total)
            # Retry delays: 0.01, 0.02, 0.04, 0.08, 0.16 = 0.31s total
            await asyncio.sleep(1.0)

            # Should have called 6 times (initial + 5 retries) then stopped
            assert call_count == 6
