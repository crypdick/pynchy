"""Behavior tests for queue policy admission and activity reporting."""

from __future__ import annotations

import asyncio

import pytest

from pynchy.turn_outcomes import TurnOutcome
from tests.group_queue_support import _runtime, _target, start_queued

pytest_plugins = ("tests.group_queue_support",)


@pytest.mark.asyncio
async def test_paused_runtime_retains_messages_and_tasks_until_resume(queue) -> None:
    target = _target("paused@g.us", "paused")
    queue.set_process_messages_fn(lambda _jid: asyncio.sleep(0, result=TurnOutcome.COMPLETED))
    await queue.pause_runtime_policy((target,))

    assert queue.is_runtime_policy_paused(target.id) is True
    await start_queued(queue.run_serialized_task(target, "task-1", lambda: asyncio.sleep(0)))
    await start_queued(queue.run_message_turn(target))
    assert queue.snapshot()["paused"] == {
        "chat_jid": "paused@g.us",
        "folder": "paused",
        "active": False,
        "is_task": False,
        "pending_messages": True,
        "pending_tasks": 1,
    }

    queue.resume_runtime_policy((target.id,))
    await asyncio.sleep(0.05)

    assert queue.is_runtime_policy_paused(target.id) is False
    assert queue.has_activity(target.id) is False
    await queue.shutdown()


@pytest.mark.asyncio
async def test_duplicate_policy_pause_is_rejected_until_resumed(queue) -> None:
    target = _target("paused@g.us", "paused")
    await queue.pause_runtime_policy((target,))

    with pytest.raises(RuntimeError, match="policy refresh already active"):
        await queue.pause_runtime_policy((target,))

    queue.resume_runtime_policy((target.id,))
    assert queue.is_runtime_policy_paused(target.id) is False


@pytest.mark.asyncio
async def test_has_activity_distinguishes_unknown_and_active_runtimes(queue) -> None:
    target = _target("active@g.us", "active")
    release = asyncio.Event()

    async def task() -> None:
        await release.wait()

    assert queue.has_activity(_runtime("missing")) is False
    await start_queued(queue.run_serialized_task(target, "task-1", task))
    await asyncio.sleep(0)
    assert queue.has_activity(target.id) is True

    release.set()
    await asyncio.sleep(0.05)
    assert queue.has_activity(target.id) is False


@pytest.mark.asyncio
async def test_message_processing_without_a_callback_returns_retry(queue) -> None:
    target = _target("uncallbacked@g.us", "uncallbacked")
    assert await queue.run_message_turn(target) is TurnOutcome.RETRY

    assert queue.snapshot()[target.folder]["active"] is False
    await queue.shutdown()
