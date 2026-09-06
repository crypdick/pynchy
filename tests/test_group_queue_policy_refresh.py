"""Runtime-policy boundary tests for the group queue."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from pynchy.turn_outcomes import TurnOutcome
from tests.group_queue_support import _target, start_queued

if TYPE_CHECKING:
    from pynchy.host.orchestrator.api import GroupQueue

pytest_plugins = ("tests.group_queue_support",)


async def test_runtime_policy_pause_waits_for_boundary_and_preserves_queued_work(
    queue: GroupQueue,
) -> None:
    affected = _target("affected@g.us", "affected")
    unaffected = _target("unaffected@g.us", "unaffected")
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    unaffected_finished = asyncio.Event()
    replacement_started = asyncio.Event()
    affected_runs = 0

    async def process_messages(chat_jid: str) -> TurnOutcome:
        nonlocal affected_runs
        if chat_jid == unaffected.chat_jid:
            unaffected_finished.set()
            return TurnOutcome.COMPLETED
        affected_runs += 1
        if affected_runs == 1:
            first_started.set()
            await release_first.wait()
        else:
            replacement_started.set()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    await start_queued(queue.run_message_turn(affected))
    await first_started.wait()

    pause = asyncio.create_task(queue.pause_runtime_policy((affected,)))
    await start_queued(queue.run_message_turn(affected))
    await start_queued(queue.run_message_turn(unaffected))
    await unaffected_finished.wait()
    assert pause.done() is False

    release_first.set()
    await pause
    assert queue.is_runtime_policy_paused(affected.id) is True
    assert affected_runs == 1

    queue.resume_runtime_policy((affected.id,))
    await replacement_started.wait()
    assert affected_runs == 2


async def test_cancelled_runtime_policy_pause_restores_admission(
    queue: GroupQueue,
) -> None:
    target = _target("affected@g.us", "affected")
    started = asyncio.Event()
    release = asyncio.Event()
    replacement_started = asyncio.Event()
    runs = 0

    async def process_messages(_chat_jid: str) -> TurnOutcome:
        nonlocal runs
        runs += 1
        if runs == 1:
            started.set()
            await release.wait()
        else:
            replacement_started.set()
        return TurnOutcome.COMPLETED

    queue.set_process_messages_fn(process_messages)
    await start_queued(queue.run_message_turn(target))
    await started.wait()
    pause = asyncio.create_task(queue.pause_runtime_policy((target,)))
    await asyncio.sleep(0)

    pause.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pause
    await start_queued(queue.run_message_turn(target))
    release.set()
    await replacement_started.wait()

    assert queue.is_runtime_policy_paused(target.id) is False
