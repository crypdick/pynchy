"""Shared helpers for integration service-tool handlers.

Service tools are ``async (data: dict) -> {"result": ...} | {"error": ...}``
handlers registered via the ``pynchy_service_handler`` hook.  The
:func:`service_tool` decorator gives every handler a uniform error envelope
so each handler body can express only its happy path (and ``raise`` on
failure) instead of hand-rolling a try/except.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from pynchy.logger import logger

ServiceHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def service_tool(handler: ServiceHandler) -> ServiceHandler:
    """Wrap a service-tool handler so uncaught exceptions become ``{"error": ...}``.

    The log line is derived from the handler name (``_handle_create_event`` →
    ``create_event``), keeping per-tool diagnostics without per-handler
    boilerplate.
    """

    op = handler.__name__.removeprefix("_handle_")

    # Excludes __name__/__qualname__ from the copied attributes (unlike plain
    # functools.wraps): beartype's claw decorates this nested `wrapper` closure
    # using its true name and qualname (``wrapper`` / ``service_tool.<locals>.wrapper``)
    # to resolve forward references via the enclosing frame, and cross-checks that
    # the last segment of __qualname__ matches __name__. Overwriting either with
    # the handler's identity (e.g. ``handle_x_post``) breaks that check and raises
    # BeartypeDecorHintForwardRefException on every call.
    @functools.wraps(handler, assigned=("__module__", "__annotations__", "__doc__"))
    async def wrapper(data: dict[str, Any]) -> dict[str, Any]:
        try:
            return await handler(data)
        except Exception as exc:  # noqa: BLE001, RUF100 - service-tool boundary converts handler failures into caller-facing errors.
            logger.error("service tool failed", op=op, error=str(exc))
            return {"error": str(exc)}

    return wrapper
