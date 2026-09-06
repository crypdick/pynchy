"""Tests for the group queue."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.turn_outcomes import TurnOutcome
from tests.group_queue_support import (
    _patch_settings,
    _runtime,
    _target,
    start_queued,
)

pytest_plugins = ("tests.group_queue_support",)

TASK_EXPLODED_MESSAGE = "task exploded"

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from pynchy.host.orchestrator.api import ContainerRuntimeOperations
    from pynchy.host.orchestrator.concurrency import GroupQueue
    from pynchy.identifiers import RuntimeId


class TestGroupQueue:
    def test_idle_runtime_rebinds_to_replacement_jid(self, queue: GroupQueue) -> None:
        """A stable runtime may adopt a replacement control address while idle."""
        first_lease = queue.acquire_host_process(_target("old-channel@g.us", "shared-runtime"))
        queue.release_host_process(first_lease)

        replacement_lease = queue.acquire_host_process(
            _target("replacement-channel@g.us", "shared-runtime")
        )

        assert queue.snapshot()["shared-runtime"]["chat_jid"] == "replacement-channel@g.us"

        queue.release_host_process(replacement_lease)

    def test_active_runtime_rejects_replacement_jid(self, queue: GroupQueue) -> None:
        """A live runtime must not change its control address mid-turn."""
        lease = queue.acquire_host_process(_target("old-channel@g.us", "shared-runtime"))

        with pytest.raises(RuntimeError, match="Cannot rebind active runtime"):
            queue.acquire_host_process(_target("replacement-channel@g.us", "shared-runtime"))

        queue.release_host_process(lease)

    async def test_same_runtime_tasks_serialize(self, queue: GroupQueue) -> None:
        """Admissions for one folder/current JID never overlap."""
        target = _target("current-channel@g.us", "shared-runtime")
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()
        active_count = 0
        max_active = 0

        async def first() -> str:
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            first_started.set()
            await release_first.wait()
            active_count -= 1
            return "first"

        async def second() -> str:
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            second_started.set()
            await asyncio.sleep(0)
            active_count -= 1
            return "second"

        first_task = asyncio.create_task(queue.run_serialized_task(target, "first", first))
        await first_started.wait()
        second_task = asyncio.create_task(queue.run_serialized_task(target, "second", second))
        await asyncio.sleep(0)

        assert second_started.is_set() is False

        release_first.set()
        assert await first_task == "first"
        assert await second_task == "second"
        assert max_active == 1

    async def test_cancelled_queued_task_never_invokes_function(self, queue: GroupQueue) -> None:
        """Cancellation while waiting must remove the queued execution."""
        target = _target("current-channel@g.us", "shared-runtime")
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()
        cancelled_fn_called = False

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        async def cancelled_fn() -> None:
            nonlocal cancelled_fn_called
            cancelled_fn_called = True
            await asyncio.sleep(0)

        blocking_task = asyncio.create_task(queue.run_serialized_task(target, "blocker", blocker))
        await blocker_started.wait()
        cancelled_task = asyncio.create_task(
            queue.run_serialized_task(target, "cancelled", cancelled_fn)
        )
        await asyncio.sleep(0)

        cancelled_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task

        release_blocker.set()
        await blocking_task
        await asyncio.sleep(0)

        assert cancelled_fn_called is False

    async def test_active_task_id_rejects_duplicate_serialized_owner(
        self, queue: GroupQueue
    ) -> None:
        target = _target("current-channel@g.us", "shared-runtime")
        started = asyncio.Event()
        release = asyncio.Event()

        async def first() -> str:
            started.set()
            await release.wait()
            return "first"

        first_owner = asyncio.create_task(queue.run_serialized_task(target, "same-task", first))
        await started.wait()

        with pytest.raises(RuntimeError, match="rejected scheduled task"):
            await queue.run_serialized_task(
                target,
                "same-task",
                lambda: asyncio.sleep(0, result="duplicate"),
            )

        release.set()
        assert await first_owner == "first"

    async def test_clearing_pending_task_cancels_its_waiting_owner(self, queue: GroupQueue) -> None:
        target = _target("current-channel@g.us", "shared-runtime")
        blocker_started = asyncio.Event()
        release_blocker = asyncio.Event()

        async def blocker() -> None:
            blocker_started.set()
            await release_blocker.wait()

        blocker_owner = asyncio.create_task(queue.run_serialized_task(target, "blocker", blocker))
        await blocker_started.wait()
        pending_owner = asyncio.create_task(
            queue.run_serialized_task(
                target,
                "pending",
                lambda: asyncio.sleep(0),
            )
        )
        await asyncio.sleep(0)

        queue.clear_pending_tasks(target.id)

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending_owner, timeout=0.5)

        release_blocker.set()
        await blocker_owner

    async def test_cancelling_active_serialized_task_stops_process_first(
        self,
        queue: GroupQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = _target("current-channel@g.us", "shared-runtime")
        started = asyncio.Event()
        cancelled = asyncio.Event()
        events: list[str] = []

        async def run() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("runner-cancelled")
                cancelled.set()
                raise

        async def stop_process(_runtime_id: RuntimeId) -> None:
            events.append("process-stopped")
            await asyncio.sleep(0)

        monkeypatch.setattr(queue, "stop_active_process", stop_process)
        owner = asyncio.create_task(queue.run_serialized_task(target, "active", run))
        await started.wait()

        owner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await cancelled.wait()

        assert events == ["process-stopped", "runner-cancelled"]

    async def test_host_process_is_visible_to_inbound_routing(self, queue: GroupQueue) -> None:
        """Temporal-run host processes must be active even outside queue dispatch."""
        target = _target("group1@g.us", "group-one")
        lease = queue.acquire_host_process(target)
        assert queue.register_host_process(lease, None, "host-agent-runner")

        queue.defer_interrupt_until_tool_result(_runtime("group-one"))
        queue.set_process_messages_fn(AsyncMock(return_value=TurnOutcome.COMPLETED))
        follow_up = await start_queued(queue.run_message_turn(target))

        assert queue.has_active_host_process("group-one") is True
        assert queue.snapshot()["group-one"]["pending_messages"] is True

        assert queue.release_host_process(lease) is True

        assert queue.has_active_host_process("group-one") is False
        assert await follow_up is TurnOutcome.COMPLETED
        assert queue.snapshot()["group-one"]["active"] is False

    def test_stale_external_process_release_preserves_a_newer_lease(
        self, queue: GroupQueue
    ) -> None:
        """Late cleanup must not clear the state adopted by a newer host process."""
        first_lease = queue.acquire_host_process(_target("group1@g.us", "group-one"))
        queue.release_host_process(first_lease)

        second_lease = queue.acquire_host_process(_target("group1@g.us", "group-one"))
        assert queue.register_host_process(second_lease, None, "host-agent-runner")

        assert queue.release_host_process(first_lease) is False
        assert queue.has_active_host_process("group-one") is True
        assert queue.release_host_process(second_lease) is False

    async def test_host_process_attaches_to_active_queue_slot(self, queue: GroupQueue) -> None:
        """A queued turn exposes its direct-host worker without taking a second slot."""
        task_complete = asyncio.Event()

        async def task_fn() -> None:
            await task_complete.wait()

        target = _target("group1@g.us", "group-one")
        await start_queued(queue.run_serialized_task(target, "task-1", task_fn))
        await asyncio.sleep(0.02)
        assert queue.is_active_task(_runtime("group-one")) is True

        lease = queue.acquire_host_process(target)
        assert lease.owns_slot is False
        assert queue.register_host_process(
            lease,
            None,
            "host-agent-runner",
        )
        assert queue.release_host_process(lease) is False
        assert queue.is_active_task(_runtime("group-one")) is True

        task_complete.set()
        await asyncio.sleep(0.05)

    async def test_shutdown_stops_host_runner_process_group(
        self, queue: GroupQueue, container_runtime: ContainerRuntimeOperations
    ) -> None:
        """Shutdown must stop a host runner through its process group, not Docker."""
        lease = queue.acquire_host_process(_target("group1@g.us", "pynchy-dev"))
        proc = AsyncMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        assert queue.register_host_process(lease, proc, "host-agent-runner")

        with patch(
            "pynchy.host.orchestrator.runtime_process_control.stop_host_process",
            new_callable=AsyncMock,
        ) as stop_host:
            await queue.shutdown()

        stop_host.assert_awaited_once_with(proc)
        container_runtime.graceful_stop.assert_not_awaited()

    async def test_shutdown_cancels_interactive_turn_waiter(self, queue: GroupQueue) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def process_messages(_chat_jid: str) -> TurnOutcome:
            started.set()
            await release.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)
        waiting_owner = asyncio.create_task(
            queue.run_message_turn(_target("group1@g.us", "group1"))
        )
        await started.wait()
        pending = await start_queued(queue.run_message_turn(_target("group1@g.us", "group1")))
        autonomous = AsyncMock()
        task = await start_queued(
            queue.run_serialized_task(_target("group1@g.us", "group1"), "pending", autonomous)
        )

        await queue.shutdown()

        for owner in (waiting_owner, pending, task):
            with pytest.raises(asyncio.CancelledError):
                await owner
        autonomous.assert_not_awaited()

        release.set()
        await asyncio.sleep(0)

    async def test_only_runs_one_container_per_group(self, queue: GroupQueue):
        concurrent_count = 0
        max_concurrent = 0

        async def process_messages(group_jid: str) -> TurnOutcome:
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)
            concurrent_count -= 1
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)

        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await start_queued(queue.run_message_turn(_target("group1@g.us")))

        # Let processing complete
        await asyncio.sleep(0.2)

        assert max_concurrent == 1

    async def test_respects_global_concurrency_limit(self, queue: GroupQueue):
        active_count = 0
        max_active = 0
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            nonlocal active_count, max_active
            active_count += 1
            max_active = max(max_active, active_count)
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            active_count -= 1
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)

        # Enqueue 3 groups (limit is 2)
        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await start_queued(queue.run_message_turn(_target("group2@g.us")))
        await start_queued(queue.run_message_turn(_target("group3@g.us")))

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

        async def process_messages(group_jid: str) -> TurnOutcome:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await first_blocker.wait()
            execution_order.append("messages")
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)

        # Start processing messages (takes the active slot)
        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await asyncio.sleep(0.02)

        # While active, enqueue both a task and pending messages
        def task_fn() -> Awaitable[None]:
            execution_order.append("task")
            return asyncio.sleep(0)

        await start_queued(queue.run_serialized_task(_target("group1@g.us"), "task-1", task_fn))
        await start_queued(queue.run_message_turn(_target("group1@g.us")))

        # Release the first processing
        first_blocker.set()
        await asyncio.sleep(0.1)

        assert execution_order[0] == "messages"  # first call
        # Messages drain before tasks — a human is waiting, tasks are autonomous
        assert execution_order[1] == "messages"  # pending messages drain first
        assert execution_order[2] == "task"  # then pending task

    async def test_prevents_new_enqueues_after_shutdown(self, queue: GroupQueue):
        process_messages = AsyncMock(return_value=TurnOutcome.COMPLETED)
        queue.set_process_messages_fn(process_messages)

        await queue.shutdown()

        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await asyncio.sleep(0.05)

        process_messages.assert_not_called()

    async def test_drains_waiting_groups_when_slots_free(self, queue: GroupQueue):
        processed: list[str] = []
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            processed.append(group_jid)
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)

        # Fill both slots
        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await start_queued(queue.run_message_turn(_target("group2@g.us")))
        await asyncio.sleep(0.05)

        # Queue a third
        await start_queued(queue.run_message_turn(_target("group3@g.us")))
        await asyncio.sleep(0.05)

        assert processed == ["group1@g.us", "group2@g.us"]

        # Free up a slot
        completions[0].set()
        await asyncio.sleep(0.05)

        assert "group3@g.us" in processed


class TestTaskAdmission:
    """Tests for task enqueuing: deduplication, shutdown guard, and concurrency."""

    async def test_queued_task_id_rejects_duplicate_owner(self, queue: GroupQueue):
        """Same task_id enqueued twice should not create duplicate entries."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)

        # Start a message to occupy the group's active slot
        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await asyncio.sleep(0.02)

        # Enqueue the same task twice while group is active
        task_calls = 0

        def task_fn() -> Awaitable[None]:
            nonlocal task_calls
            task_calls += 1
            return asyncio.sleep(0)

        await start_queued(queue.run_serialized_task(_target("group1@g.us"), "task-1", task_fn))
        with pytest.raises(RuntimeError, match="rejected scheduled task"):
            await queue.run_serialized_task(_target("group1@g.us"), "task-1", task_fn)

        # Release and let everything drain
        completions[0].set()
        await asyncio.sleep(0.15)

        # Task should only have run once
        assert task_calls == 1

    async def test_different_task_ids_both_queued(self, queue: GroupQueue):
        """Different task IDs for the same group should both be queued."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)

        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await asyncio.sleep(0.02)

        task_ids_run: list[str] = []

        def task_a() -> Awaitable[None]:
            task_ids_run.append("a")
            return asyncio.sleep(0)

        def task_b() -> Awaitable[None]:
            task_ids_run.append("b")
            return asyncio.sleep(0)

        await start_queued(queue.run_serialized_task(_target("group1@g.us"), "task-a", task_a))
        await start_queued(queue.run_serialized_task(_target("group1@g.us"), "task-b", task_b))

        completions[0].set()
        await asyncio.sleep(0.15)

        assert set(task_ids_run) == {"a", "b"}

    async def test_enqueue_task_blocked_after_shutdown(self, queue: GroupQueue):
        """Tasks should be silently dropped after shutdown."""
        process_messages = AsyncMock(return_value=TurnOutcome.COMPLETED)
        queue.set_process_messages_fn(process_messages)

        await queue.shutdown()

        task_called = False

        def task_fn() -> Awaitable[None]:
            nonlocal task_called
            task_called = True
            return asyncio.sleep(0)

        await start_queued(queue.run_serialized_task(_target("group1@g.us"), "task-1", task_fn))
        await asyncio.sleep(0.05)

        assert task_called is False


class TestSendMessage:
    """Tests for send_message: IPC file write for active containers."""

    def test_returns_false_when_group_not_active(self, queue: GroupQueue):
        assert queue.send_message(_runtime("group1@g.us"), "hello") is False

    async def test_defers_followup_while_interactive_turn_is_active(
        self, queue: GroupQueue, container_runtime: ContainerRuntimeOperations
    ):
        completion = asyncio.Event()

        async def process_messages(_group_jid: str) -> TurnOutcome:
            await completion.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)
        await start_queued(queue.run_message_turn(_target("group1@g.us", "test-group")))
        await asyncio.sleep(0.02)
        queue.register_process(_runtime("test-group"), None, "container-1")

        assert queue.send_message(_runtime("test-group"), "follow-up") is False
        container_runtime.write_message.assert_not_called()

        completion.set()
        await asyncio.sleep(0.05)

    async def test_writes_ipc_file_for_active_scheduled_task(
        self, queue: GroupQueue, container_runtime: ContainerRuntimeOperations
    ):
        """Scheduled tasks accept best-effort context through IPC."""
        completion = asyncio.Event()

        async def run_task() -> None:
            await completion.wait()

        await start_queued(
            queue.run_serialized_task(_target("group1@g.us", "test-group"), "task-1", run_task)
        )
        await asyncio.sleep(0.02)

        queue.register_process(_runtime("test-group"), None, "container-1")

        result = queue.send_message(_runtime("test-group"), "hello world")

        assert result is True
        container_runtime.write_message.assert_called_once_with("test-group", "hello world")

        completion.set()
        await asyncio.sleep(0.05)


class TestSnapshot:
    """Tests for snapshot(): read-only view of queue state."""

    def test_empty_queue_snapshot(self, queue: GroupQueue):
        """Empty queue returns only _meta with zero counts."""
        snap = queue.snapshot()
        assert snap == {"_meta": {"active_count": 0, "waiting_count": 0}}

    async def test_snapshot_reflects_active_group(self, queue: GroupQueue):
        """Snapshot includes active groups with their state."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)
        await start_queued(queue.run_message_turn(_target("group1@g.us", "test-group")))
        await asyncio.sleep(0.02)

        snap = queue.snapshot()
        assert snap["test-group"] == {
            "chat_jid": "group1@g.us",
            "folder": "test-group",
            "active": True,
            "is_task": False,
            "pending_messages": False,
            "pending_tasks": 0,
        }
        assert snap["_meta"]["active_count"] == 1

        completions[0].set()
        await asyncio.sleep(0.05)

    async def test_snapshot_shows_pending_and_waiting(self, queue: GroupQueue):
        """Snapshot shows pending messages and waiting groups correctly."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)

        # Fill both slots (max_concurrent=2)
        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await start_queued(queue.run_message_turn(_target("group2@g.us")))
        await asyncio.sleep(0.02)

        # Queue a third group (should go to waiting)
        await start_queued(queue.run_message_turn(_target("group3@g.us")))

        snap = queue.snapshot()
        assert snap["_meta"]["active_count"] == 2
        assert snap["_meta"]["waiting_count"] == 1
        assert snap["group3@g.us"]["pending_messages"] is True

        # Clean up
        for evt in completions:
            evt.set()
        await asyncio.sleep(0.1)

    async def test_snapshot_shows_pending_tasks(self, queue: GroupQueue):
        """Snapshot reflects queued tasks for an active group."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)
        await start_queued(queue.run_message_turn(_target("group1@g.us")))
        await asyncio.sleep(0.02)

        def task_fn() -> Awaitable[None]:
            return asyncio.sleep(0)

        await start_queued(queue.run_serialized_task(_target("group1@g.us"), "task-1", task_fn))
        await start_queued(queue.run_serialized_task(_target("group1@g.us"), "task-2", task_fn))

        snap = queue.snapshot()
        assert snap["group1@g.us"]["pending_tasks"] == 2

        completions[0].set()
        await asyncio.sleep(0.15)


class TestCloseStdin:
    """Tests for close_stdin: writing _close sentinel to active containers."""

    async def test_writes_close_sentinel_when_active(
        self, queue: GroupQueue, container_runtime: ContainerRuntimeOperations
    ):
        """close_stdin delegates the close sentinel to the bound runtime."""
        completions: list[asyncio.Event] = []

        async def process_messages(group_jid: str) -> TurnOutcome:
            event = asyncio.Event()
            completions.append(event)
            await event.wait()
            return TurnOutcome.COMPLETED

        queue.set_process_messages_fn(process_messages)
        await start_queued(queue.run_message_turn(_target("group1@g.us", "test-group")))
        await asyncio.sleep(0.02)

        queue.register_process(_runtime("test-group"), None, "container-1")

        queue.close_stdin(_runtime("test-group"))

        container_runtime.write_close_sentinel.assert_called_once_with("test-group")

        completions[0].set()
        await asyncio.sleep(0.05)

    async def test_noop_when_not_active(self, queue: GroupQueue, tmp_path):
        """close_stdin does nothing when group is not active."""
        with _patch_settings(data_dir=tmp_path):
            queue.close_stdin(_runtime("group1@g.us"))

        # No IPC directory should be created
        ipc_dir = tmp_path / "ipc"
        assert not ipc_dir.exists()
