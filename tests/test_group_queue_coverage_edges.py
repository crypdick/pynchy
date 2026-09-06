"""Public behavior coverage for runtime queue boundaries."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.identifiers import RuntimeId
from pynchy.turn_outcomes import TurnOutcome
from tests.group_queue_support import _runtime, _target, start_queued

pytest_plugins = ("tests.group_queue_support",)


def _limited_queue(container_runtime) -> GroupQueue:
    return GroupQueue(
        1,
        container_runtime,
    )


@pytest.mark.parametrize("interactive", [False, True])
async def test_cancellation_preserves_owner_when_process_finishes_during_stop(
    container_runtime, interactive
):
    queue = _limited_queue(container_runtime)
    target = _target("cancel@g.us", "cancel")
    started = asyncio.Event()
    finish = asyncio.Event()
    finished = asyncio.Event()

    async def process(_jid: str) -> TurnOutcome:
        started.set()
        await finish.wait()
        finished.set()
        return TurnOutcome.COMPLETED

    async def stop(_proc, _name):
        finish.set()
        await finished.wait()

    queue.set_process_messages_fn(process)
    container_runtime.graceful_stop.side_effect = stop
    work = (
        queue.run_message_turn(target)
        if interactive
        else queue.run_serialized_task(target, "task", lambda: process(target.chat_jid))
    )
    owner = asyncio.create_task(work)
    await started.wait()
    queue.register_process(
        target.id, MagicMock(spec=asyncio.subprocess.Process, returncode=None), "container"
    )
    owner.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert queue.has_activity(target.id) is False
    finally:
        await queue.shutdown()


async def test_cleanup_failure_reaches_owner_and_releases_the_slot(container_runtime):
    queue = _limited_queue(container_runtime)
    container_runtime.clean_input_dir.side_effect = [OSError("cleanup failed"), None]
    target = _target("cleanup@g.us", "cleanup")

    with pytest.raises(OSError, match="cleanup failed"):
        await queue.run_serialized_task(target, "first", lambda: asyncio.sleep(0, result="done"))

    assert queue.snapshot()["_meta"]["active_count"] == 0
    assert (
        await queue.run_serialized_task(target, "second", lambda: asyncio.sleep(0, result="next"))
        == "next"
    )
    await queue.shutdown()


@pytest.mark.asyncio
async def test_shutdown_waits_for_active_queue_work_to_stop(container_runtime):
    queue = _limited_queue(container_runtime)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def process_messages(_jid: str) -> TurnOutcome:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    await start_queued(queue.run_message_turn(_target("active@g.us", "active")))
    await started.wait()

    await queue.shutdown()

    assert stopped.is_set()


@pytest.mark.asyncio
async def test_active_folders_reports_running_targets(container_runtime):
    queue = _limited_queue(container_runtime)
    started = asyncio.Event()
    release = asyncio.Event()

    async def run() -> None:
        started.set()
        await release.wait()

    await start_queued(queue.run_serialized_task(_target("active@g.us", "active"), "active", run))
    await started.wait()

    assert queue.active_folders() == {"active"}

    release.set()
    await asyncio.sleep(0.05)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_global_limit_defers_task_until_active_runtime_finishes(container_runtime):
    queue = _limited_queue(container_runtime)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def first() -> None:
        first_started.set()
        await release_first.wait()

    await start_queued(queue.run_serialized_task(_target("first@g.us", "first"), "first", first))
    await first_started.wait()

    second = _target("second@g.us", "second")
    await start_queued(queue.run_serialized_task(second, "second", lambda: asyncio.sleep(0)))
    assert queue.snapshot()["second"]["pending_tasks"] == 1
    assert queue.snapshot()["_meta"]["waiting_count"] == 1

    release_first.set()
    await asyncio.sleep(0.05)
    assert queue.has_activity(second.id) is False
    await queue.shutdown()


@pytest.mark.asyncio
async def test_global_waiting_queue_deduplicates_message_and_task_runtime(container_runtime):
    queue = _limited_queue(container_runtime)
    started = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        started.set()
        await release.wait()

    await start_queued(queue.run_serialized_task(_target("first@g.us", "first"), "first", first))
    await started.wait()
    second = _target("second@g.us", "second")

    await start_queued(queue.run_message_turn(second))
    await start_queued(queue.run_message_turn(second))
    await start_queued(queue.run_serialized_task(second, "second", lambda: asyncio.sleep(0)))
    with pytest.raises(RuntimeError, match="rejected scheduled task"):
        await queue.run_serialized_task(second, "second", lambda: asyncio.sleep(0))
    assert queue.snapshot()["_meta"]["waiting_count"] == 1

    release.set()
    await asyncio.sleep(0.05)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_global_limit_defers_message_until_active_runtime_finishes(container_runtime):
    queue = _limited_queue(container_runtime)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def process_messages(_jid: str) -> TurnOutcome:
        first_started.set()
        await release_first.wait()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    first = _target("first@g.us", "first")
    second = _target("second@g.us", "second")
    await start_queued(queue.run_message_turn(first))
    await first_started.wait()

    await start_queued(queue.run_message_turn(second))
    assert queue.snapshot()["second"]["pending_messages"] is True
    assert queue.snapshot()["_meta"]["waiting_count"] == 1

    release_first.set()
    await asyncio.sleep(0.05)
    assert queue.has_activity(second.id) is False
    await queue.shutdown()


@pytest.mark.asyncio
async def test_safe_interrupt_returns_control_to_its_workflow(container_runtime):
    queue = _limited_queue(container_runtime)
    calls = 0

    async def process_messages(_jid: str) -> TurnOutcome:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT if calls == 1 else TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    result = await queue.run_message_turn(_target("interrupt@g.us", "interrupt"))
    await asyncio.sleep(0.05)

    assert result is TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT
    assert calls == 1
    await queue.shutdown()


@pytest.mark.asyncio
async def test_message_processing_exception_releases_slot_without_retry(container_runtime):
    queue = _limited_queue(container_runtime)

    async def process_messages(_jid: str) -> TurnOutcome:
        await asyncio.sleep(0)
        raise ValueError("processing failed")

    queue.set_process_messages_fn(process_messages)
    with pytest.raises(ValueError, match="processing failed"):
        await queue.run_message_turn(_target("failure@g.us", "failure"))

    assert queue.snapshot()["failure"]["active"] is False
    await queue.shutdown()


@pytest.mark.asyncio
async def test_clearing_pending_work_handles_known_and_unknown_runtime(container_runtime):
    queue = _limited_queue(container_runtime)
    started = asyncio.Event()
    release = asyncio.Event()

    async def process_messages(_jid: str) -> TurnOutcome:
        started.set()
        await release.wait()
        return TurnOutcome.COMPLETED

    target = _target("clear@g.us", "clear")
    queue.set_process_messages_fn(process_messages)
    await start_queued(queue.run_message_turn(target))
    await started.wait()
    await start_queued(queue.run_serialized_task(target, "pending", lambda: asyncio.sleep(0)))
    await start_queued(queue.run_message_turn(target))

    queue.clear_pending_tasks(target.id)
    queue.clear_pending_messages(target.id)
    queue.clear_pending_tasks(RuntimeId("missing"))
    queue.clear_pending_messages(RuntimeId("missing"))
    assert queue.snapshot()["clear"]["pending_messages"] is False

    release.set()
    await asyncio.sleep(0.05)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_resuming_unknown_runtime_is_a_no_op(container_runtime):
    queue = _limited_queue(container_runtime)

    queue.resume_runtime_policy((RuntimeId("missing"),))

    snapshot = queue.snapshot()
    assert "missing" not in snapshot
    assert snapshot["_meta"]["waiting_count"] == 0
    await queue.shutdown()


@pytest.mark.asyncio
async def test_cancelled_message_waiter_is_skipped_during_runtime_cleanup(container_runtime):
    queue = _limited_queue(container_runtime)
    started = asyncio.Event()
    release = asyncio.Event()

    async def process_messages(_jid: str) -> TurnOutcome:
        started.set()
        await release.wait()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    target = _target("cancelled@g.us", "cancelled")
    turn = asyncio.create_task(queue.run_message_turn(target))
    await started.wait()
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn

    release.set()
    await asyncio.sleep(0.05)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_task_exception_releases_slot_and_cleans_runtime_input(container_runtime):
    queue = _limited_queue(container_runtime)

    async def failed_task() -> None:
        await asyncio.sleep(0)
        raise ValueError("task failed")

    target = _target("task-failure@g.us", "task-failure")
    with pytest.raises(ValueError, match="task failed"):
        await queue.run_serialized_task(target, "failed", failed_task)

    assert queue.has_activity(target.id) is False
    container_runtime.clean_input_dir.assert_called_once_with("task-failure")
    await queue.shutdown()


@pytest.mark.asyncio
async def test_cancelled_serialized_task_is_removed_after_other_pending_work(container_runtime):
    queue = _limited_queue(container_runtime)
    started = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        started.set()
        await release.wait()

    first_target = _target("first@g.us", "first")
    second_target = _target("second@g.us", "second")
    await start_queued(queue.run_serialized_task(first_target, "first", first))
    await started.wait()
    await start_queued(queue.run_serialized_task(second_target, "manual", lambda: asyncio.sleep(0)))

    waiting = asyncio.create_task(
        queue.run_serialized_task(second_target, "cancelled", lambda: asyncio.sleep(0))
    )
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    release.set()
    await asyncio.sleep(0.05)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_interrupt_after_tool_result_stops_active_runtime(container_runtime):
    queue = _limited_queue(container_runtime)
    started = asyncio.Event()
    release = asyncio.Event()

    async def process_messages(_jid: str) -> TurnOutcome:
        started.set()
        await release.wait()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    target = _target("tool-boundary@g.us", "tool-boundary")
    await start_queued(queue.run_message_turn(target))
    await started.wait()
    queue.defer_interrupt_until_tool_result(target.id)

    assert await queue.interrupt_after_tool_result(target.id) is True
    assert queue.boundary_interrupt_requested(target.id) is True
    container_runtime.destroy_session.assert_awaited_once_with("tool-boundary")

    release.set()
    await asyncio.sleep(0.05)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_process_control_delegates_destroy_for_unknown_runtime(container_runtime):
    queue = _limited_queue(container_runtime)
    runtime_id = RuntimeId("unknown-runtime")

    await queue.destroy_runtime_session(runtime_id)
    await queue.stop_active_process(runtime_id)

    container_runtime.destroy_session.assert_awaited_once_with("unknown-runtime")
    assert await queue.interrupt_after_tool_result(runtime_id) is False
    assert queue.has_active_run(runtime_id) is False
    await queue.stop_active_process_for_control(runtime_id)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_process_control_destroys_an_idle_registered_runtime(container_runtime):
    queue = _limited_queue(container_runtime)
    runtime_id = _runtime("idle-runtime")
    await start_queued(
        queue.run_serialized_task(
            _target("idle-runtime@g.us", "idle-runtime"), "idle", lambda: asyncio.sleep(0)
        )
    )
    await asyncio.sleep(0.05)
    assert queue.register_process(runtime_id, None, "idle-container") is True

    await queue.stop_active_process(runtime_id)

    container_runtime.destroy_session.assert_awaited_once_with("idle-runtime")

    host_target = _target("host-idle@g.us", "host-idle")
    lease = queue.acquire_host_process(host_target)
    assert queue.register_host_process(lease, None, "host-container") is True
    await queue.stop_active_process(host_target.id)
    assert queue.release_host_process(lease) is False
    await queue.shutdown()
