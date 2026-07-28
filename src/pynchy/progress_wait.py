"""Async progress-aware deadline handling."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import (  # noqa: TC003 - beartype resolves this runtime annotation.
    Awaitable,
)

_PROGRESS_HARD_TIMEOUT_MULTIPLIER = 4.0


class ProgressTimeoutError(TimeoutError):
    """Raised when an operation exceeds its silence or hard deadline."""

    def __init__(
        self,
        reason: str,
        *,
        inactivity_timeout_seconds: float,
        hard_timeout_seconds: float,
    ) -> None:
        self.reason = reason
        self.inactivity_timeout_seconds = inactivity_timeout_seconds
        self.hard_timeout_seconds = hard_timeout_seconds
        super().__init__(
            f"operation exceeded {reason} timeout "
            f"(inactivity={inactivity_timeout_seconds}s, hard={hard_timeout_seconds}s)"
        )


async def wait_for_progress[ResultT](
    operation: Awaitable[ResultT],
    *,
    progress_event: asyncio.Event,
    inactivity_timeout_seconds: int | float,
    hard_timeout_seconds: int | float | None = None,
) -> ResultT:
    """Wait for completion while activity refreshes a bounded silence deadline."""
    effective_inactivity_timeout = float(inactivity_timeout_seconds)
    effective_hard_timeout = float(
        effective_inactivity_timeout * _PROGRESS_HARD_TIMEOUT_MULTIPLIER
        if hard_timeout_seconds is None
        else hard_timeout_seconds
    )
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    inactivity_deadline = started_at + effective_inactivity_timeout
    hard_deadline = started_at + effective_hard_timeout
    operation_task = asyncio.ensure_future(operation)
    progress_task: asyncio.Task[bool] | None = None

    try:
        while True:
            if operation_task.done():
                return await operation_task

            if progress_event.is_set():
                progress_event.clear()
                inactivity_deadline = loop.time() + effective_inactivity_timeout
                continue

            now = loop.time()
            deadline = min(inactivity_deadline, hard_deadline)
            if now >= deadline:
                reason = "hard" if hard_deadline <= inactivity_deadline else "inactivity"
                raise ProgressTimeoutError(
                    reason,
                    inactivity_timeout_seconds=effective_inactivity_timeout,
                    hard_timeout_seconds=effective_hard_timeout,
                )

            progress_task = asyncio.create_task(progress_event.wait())
            done, _pending = await asyncio.wait(
                (operation_task, progress_task),
                timeout=deadline - now,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation_task in done:
                return await operation_task
            if progress_task in done:
                progress_task = None
                continue
            if progress_event.is_set():
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task
                progress_task = None
                continue

            reason = "hard" if hard_deadline <= inactivity_deadline else "inactivity"
            raise ProgressTimeoutError(
                reason,
                inactivity_timeout_seconds=effective_inactivity_timeout,
                hard_timeout_seconds=effective_hard_timeout,
            )
    finally:
        for task in (progress_task, operation_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
