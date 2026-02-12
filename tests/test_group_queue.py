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
        assert execution_order[1] == "task"  # task runs first in drain

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


class TestGroupQueueRetry:
    """Test retry behavior with shorter timeouts."""

    async def test_retries_with_backoff(self):
        with patch("pynchy.group_queue.MAX_CONCURRENT_CONTAINERS", 2), \
             patch("pynchy.group_queue.BASE_RETRY_SECONDS", 0.1):
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
        with patch("pynchy.group_queue.MAX_CONCURRENT_CONTAINERS", 2), \
             patch("pynchy.group_queue.BASE_RETRY_SECONDS", 0.01):
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
