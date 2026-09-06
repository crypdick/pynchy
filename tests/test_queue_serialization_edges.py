"""Failure-boundary contracts for queue serialization adapters."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from pynchy.host.orchestrator.queue_serialization import await_queued_task
from tests.group_queue_support import _target


@pytest.mark.asyncio
async def test_cancelled_queued_waiter_drops_runner_before_calling_function() -> None:
    target = _target("channel@g.us", "project")
    queued: dict[str, object] = {}
    called = False

    def enqueue(_target, task) -> bool:
        queued["task"] = task
        return True

    async def fn() -> None:
        nonlocal called
        called = True
        await asyncio.sleep(0)

    waiter = asyncio.create_task(
        await_queued_task(
            enqueue,
            Mock(return_value=True),
            AsyncMock(),
            target,
            "task-1",
            fn,
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await queued["task"].fn()  # type: ignore[union-attr]
    assert called is False


@pytest.mark.asyncio
async def test_queued_task_does_not_set_result_after_waiter_cancellation() -> None:
    target = _target("channel@g.us", "project")
    queued: dict[str, object] = {}

    def enqueue(_target, task) -> bool:
        queued["task"] = task
        return True

    async def fn() -> None:
        await asyncio.sleep(0)
        queued["task"].cancel()  # type: ignore[union-attr]

    waiter = asyncio.create_task(
        await_queued_task(
            enqueue,
            Mock(return_value=True),
            AsyncMock(),
            target,
            "task-1",
            fn,
        )
    )
    await asyncio.sleep(0)

    await queued["task"].fn()  # type: ignore[union-attr]
    with pytest.raises(asyncio.CancelledError):
        await waiter
