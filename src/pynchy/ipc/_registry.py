"""Handler registry for IPC task types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pynchy.ipc._deps import IpcDeps
from pynchy.logger import logger

# type -> async handler(data, source_group, is_god, deps)
HANDLERS: dict[str, Callable[[dict[str, Any], str, bool, IpcDeps], Awaitable[None]]] = {}


def register(
    type_name: str,
    handler: Callable[[dict[str, Any], str, bool, IpcDeps], Awaitable[None]],
) -> None:
    """Register a handler for an IPC task type.

    Called at module import time by each handler module to wire up their
    handlers.  Duplicate registrations silently overwrite (last-write-wins).
    """
    HANDLERS[type_name] = handler


async def dispatch(
    data: dict[str, Any],
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    """Dispatch an IPC task to its registered handler."""
    handler = HANDLERS.get(data.get("type", ""))
    if handler:
        await handler(data, source_group, is_god, deps)
    else:
        logger.warning("Unknown IPC task type", type=data.get("type"))
