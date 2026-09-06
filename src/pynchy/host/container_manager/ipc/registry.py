"""Handler registry for IPC request kinds."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from typing import Any

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.host.container_manager.ipc.protocol import IpcRequestEnvelope
from pynchy.logger import logger

# kind/type -> async handler(data, source_group, is_admin, deps)
HANDLERS: dict[str, Callable[[dict[str, Any], str, bool, IpcDeps], Awaitable[None]]] = {}

# prefix -> async handler (checked when exact match fails)
PREFIX_HANDLERS: dict[str, Callable[[dict[str, Any], str, bool, IpcDeps], Awaitable[None]]] = {}


def register(
    type_name: str,
    handler: Callable[[dict[str, Any], str, bool, IpcDeps], Awaitable[None]],
) -> None:
    """Register a handler for an IPC request kind.

    Called at module import time by each handler module to wire up their
    handlers.  Duplicate registrations silently overwrite (last-write-wins).
    """
    HANDLERS[type_name] = handler


def register_prefix(
    prefix: str,
    handler: Callable[[dict[str, Any], str, bool, IpcDeps], Awaitable[None]],
) -> None:
    """Register a handler for all IPC types matching a prefix.

    Prefix handlers are checked when no exact match is found.
    """
    PREFIX_HANDLERS[prefix] = handler


async def dispatch(
    request: IpcRequestEnvelope | dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - dispatcher callback signature is part of the IPC protocol.
    deps: IpcDeps,
) -> None:
    """Dispatch an IPC request to its registered handler."""
    data = request.to_handler_data() if isinstance(request, IpcRequestEnvelope) else request
    request_kind = data.get("type") or ""
    handler = HANDLERS.get(request_kind)
    if handler:
        await handler(data, source_group, is_admin, deps)
        return

    # Check prefix handlers
    for prefix, prefix_handler in PREFIX_HANDLERS.items():
        if request_kind.startswith(prefix):
            await prefix_handler(data, source_group, is_admin, deps)
            return

    logger.warning("Unknown IPC request kind", kind=request_kind)
