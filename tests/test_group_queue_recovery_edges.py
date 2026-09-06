"""Reachable runtime-policy and host-process recovery behavior."""

from __future__ import annotations

import asyncio

import pytest

from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.turn_outcomes import TurnOutcome
from tests.group_queue_support import _target, start_queued

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
        1,
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
    await start_queued(queue.run_message_turn(_target("first@g.us", "first")))
    await first_started.wait()

    second = _target("second@g.us", "second")
    await start_queued(queue.run_message_turn(second))
    await queue.pause_runtime_policy((second,))
    queue.resume_runtime_policy((second.id,))

    assert queue.snapshot()["_meta"]["waiting_count"] == 1
    release_first.set()
    await second_started.wait()
    await asyncio.sleep(0)
    await queue.shutdown()


@pytest.mark.asyncio
async def test_resuming_idle_runtime_at_capacity_does_not_wait(container_runtime):
    queue = GroupQueue(
        1,
        container_runtime,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def process_messages(_chat_jid: str) -> TurnOutcome:
        first_started.set()
        await release_first.wait()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    await start_queued(queue.run_message_turn(_target("first@g.us", "first")))
    await first_started.wait()

    second = _target("second@g.us", "second")
    await queue.pause_runtime_policy((second,))
    queue.resume_runtime_policy((second.id,))

    assert queue.snapshot()["_meta"]["waiting_count"] == 0
    release_first.set()
    await queue.shutdown()


@pytest.mark.asyncio
async def test_paused_waiting_runtime_stays_deferred_after_global_slot_frees(
    container_runtime,
):
    queue = GroupQueue(
        1,
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
    await start_queued(queue.run_message_turn(_target("first@g.us", "first")))
    await first_started.wait()

    second = _target("second@g.us", "second")
    await start_queued(queue.run_message_turn(second))
    await queue.pause_runtime_policy((second,))
    release_first.set()
    await asyncio.sleep(0.05)

    assert second_started.is_set() is False
    assert queue.snapshot()["second"]["pending_messages"] is True

    queue.resume_runtime_policy((second.id,))
    await asyncio.wait_for(second_started.wait(), timeout=1)
    await queue.shutdown()
