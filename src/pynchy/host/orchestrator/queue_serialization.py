"""Awaitable adapters for the per-thread work queue."""

from __future__ import annotations

import asyncio
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves queue annotations at runtime.
    Awaitable,
    Callable,
)
from typing import cast

from pynchy.host.orchestrator.messaging.outcomes import (  # noqa: TC001, RUF100 - beartype resolves queue annotations at runtime.
    ProcessGroupResult,
)
from pynchy.host.orchestrator.queue_state import (  # noqa: TC001, RUF100 - beartype resolves queue annotations at runtime.
    GroupState,
)


async def await_queued_task[ResultT](
    enqueue_task: Callable[[str, str, Callable[[], Awaitable[None]]], bool],
    group_jid: str,
    task_id: str,
    fn: Callable[[], Awaitable[ResultT]],
) -> ResultT:
    """Run scheduled work through the same per-thread queue as messages."""
    future: asyncio.Future[ResultT] = asyncio.get_running_loop().create_future()

    async def queued() -> None:
        try:
            result = await fn()
            if not future.done():
                future.set_result(result)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
            raise

    if not enqueue_task(group_jid, task_id, queued):
        raise RuntimeError(f"Thread queue rejected scheduled task {task_id}")
    return await future


async def await_message_turn(
    get_group: Callable[[str], GroupState],
    enqueue_message_check: Callable[[str], None],
    group_jid: str,
    *,
    shutting_down: bool,
) -> ProcessGroupResult:
    """Wait until a thread's queued interactive turn has executed."""
    if shutting_down:
        raise RuntimeError("Thread queue is shutting down")
    future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
    get_group(group_jid).message_waiters.append(future)
    enqueue_message_check(group_jid)
    return cast("ProcessGroupResult", await future)
