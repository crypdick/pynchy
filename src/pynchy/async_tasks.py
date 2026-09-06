"""Shared background-task creation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_LOGGER = logging.getLogger(__name__)


def create_background_task(
    coro: Coroutine[Any, Any, Any],
    *,
    name: str | None = None,
) -> asyncio.Task[Any]:
    """Create an asyncio task that logs exceptions instead of swallowing them."""
    task = asyncio.create_task(coro)
    if name is not None:
        task.set_name(name)
    task.add_done_callback(_log_task_exception)
    return task


def _log_task_exception(task: asyncio.Future[Any]) -> None:
    """Callback attached to background tasks — logs unhandled exceptions."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        # Pass the exception to exc_info so structlog renders the full
        # traceback. ``logger.exception()`` won't work here because we're
        # in a done-callback, not an except handler.
        _LOGGER.error(
            "Background task failed: task_name=%s",
            task.get_name() if isinstance(task, asyncio.Task) else None,
            exc_info=exc,
        )
