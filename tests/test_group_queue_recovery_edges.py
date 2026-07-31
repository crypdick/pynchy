"""Reachable runtime-policy and host-process recovery behavior."""

from __future__ import annotations

import asyncio

import pytest

from pynchy.host.orchestrator.concurrency import GroupQueue, QueuePolicy
from pynchy.turn_outcomes import TurnOutcome
from tests.group_queue_support import _target

pytest_plugins = ("tests.group_queue_support",)


@pytest.mark.asyncio
async def test_releasing_a_paused_host_process_completes_the_policy_boundary(
    queue,
):
    target = _target("paused-host@g.us", "paused-host")
    lease = queue.acquire_host_process(target)

    pause = asyncio.create_task(queue.pause_runtime_policy((target,)))
    await asyncio.sleep(0)
    assert pause.done() is False

    assert queue.release_host_process(lease) is False
    await pause
    assert queue.is_runtime_policy_paused(target.id) is True

    queue.resume_runtime_policy((target.id,))
    assert queue.is_runtime_policy_paused(target.id) is False
    await queue.shutdown()


@pytest.mark.asyncio
async def test_resuming_waiting_runtime_keeps_it_waiting_until_global_slot_is_free(
    container_runtime,
):
    queue = GroupQueue(
        QueuePolicy(max_concurrent=1, max_retries=0, retry_base_seconds=0.0),
        container_runtime,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def process_messages(chat_jid: str) -> TurnOutcome:
        if chat_jid == "first@g.us":
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    queue.enqueue_message_check(_target("first@g.us", "first"))
    await first_started.wait()

    second = _target("second@g.us", "second")
    queue.enqueue_message_check(second)
    await queue.pause_runtime_policy((second,))
    queue.resume_runtime_policy((second.id,))

    assert queue.snapshot()["_meta"]["waiting_count"] == 1
    release_first.set()
    await second_started.wait()
    await asyncio.sleep(0)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_shutdown_suppresses_a_scheduled_retry(container_runtime):
    queue = GroupQueue(
        QueuePolicy(max_concurrent=1, max_retries=1, retry_base_seconds=0.05),
        container_runtime,
    )

    async def process_messages(_chat_jid: str) -> TurnOutcome:
        await asyncio.sleep(0)
        return TurnOutcome.RETRY

    queue.set_process_messages_fn(process_messages)
    target = _target("retry-shutdown@g.us", "retry-shutdown")

    assert await queue.run_message_turn(target) is TurnOutcome.RETRY
    await queue.shutdown()
    await asyncio.sleep(0.06)

    assert queue.has_activity(target.id) is False
