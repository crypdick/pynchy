"""Async progress-aware deadline handling."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import (
    Awaitable,
)

_PROGRESS_HARD_TIMEOUT_MULTIPLIER = 4.0
_INITIAL_PROGRESS_TIMEOUT_SECONDS = 60.0


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


def _timeout_reason(
    initial_progress_deadline: float,
    inactivity_deadline: float,
    hard_deadline: float,
) -> str:
    if initial_progress_deadline <= min(inactivity_deadline, hard_deadline):
        return "initial_progress"
    return "hard" if hard_deadline <= inactivity_deadline else "inactivity"


async def wait_for_progress[ResultT](
    operation: Awaitable[ResultT],
    *,
    progress_event: asyncio.Event,
    inactivity_timeout_seconds: int | float,
    initial_progress_timeout_seconds: int | float = _INITIAL_PROGRESS_TIMEOUT_SECONDS,
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
    initial_progress_deadline = started_at + float(initial_progress_timeout_seconds)
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
                initial_progress_deadline = float("inf")
                inactivity_deadline = loop.time() + effective_inactivity_timeout
                continue

            now = loop.time()
            deadline = min(initial_progress_deadline, inactivity_deadline, hard_deadline)
            if now >= deadline:
                reason = _timeout_reason(
                    initial_progress_deadline,
                    inactivity_deadline,
                    hard_deadline,
                )
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

            reason = _timeout_reason(
                initial_progress_deadline,
                inactivity_deadline,
                hard_deadline,
            )
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
