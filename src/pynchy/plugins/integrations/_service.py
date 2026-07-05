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

from pynchy.logger import logger

ServiceHandler = Callable[[dict], Awaitable[dict]]


def service_tool(handler: ServiceHandler) -> ServiceHandler:
    """Wrap a service-tool handler so uncaught exceptions become ``{"error": ...}``.

    The log line is derived from the handler name (``_handle_create_event`` →
    ``create_event``), keeping per-tool diagnostics without per-handler
    boilerplate.
    """

    op = handler.__name__.removeprefix("_handle_")

    @functools.wraps(handler)
    async def wrapper(data: dict) -> dict:
        try:
            return await handler(data)
        except Exception as exc:
            logger.error("service tool failed", op=op, error=str(exc))
            return {"error": str(exc)}

    return wrapper
