"""Lifecycle contract for authenticated external-provider connections."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pluggy

from pynchy.identifiers import (
    SessionId,
)
from pynchy.plugins.contracts import (
    Channel,
    NewMessage,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)


@dataclass(frozen=True, slots=True)
class ConnectionRuntimeContext:
    """Host callbacks available to one provider connection runtime."""

    channels: Callable[[], list[Channel]]
    workspaces: Callable[[], dict[str, WorkspaceProfile]]
    register_workspace: Callable[[WorkspaceProfile], Awaitable[None]]
    unregister_workspace: Callable[[str], Awaitable[None]]
    bind_session: Callable[[str, SessionId], Awaitable[None]]
    ingest_message: Callable[[str, NewMessage], Awaitable[None]]


@runtime_checkable
class ConnectionRuntime(Protocol):
    """One named, long-running provider identity owned by a plugin."""

    name: str

    async def start(self, context: ConnectionRuntimeContext) -> None: ...

    async def close(self) -> None: ...

    def is_ready(self) -> bool: ...


def load_connection_runtimes(pm: pluggy.PluginManager) -> list[ConnectionRuntime]:
    """Collect typed runtimes and reject ambiguous connection identities."""
    runtimes: list[ConnectionRuntime] = []
    for contribution in pm.hook.pynchy_connection_runtime():
        candidates = contribution if isinstance(contribution, tuple | list) else (contribution,)
        for candidate in candidates:
            if not isinstance(candidate, ConnectionRuntime):
                raise TypeError(
                    "Connection plugins must return ConnectionRuntime instances, got "
                    f"{type(candidate).__name__}"
                )
            runtimes.append(candidate)

    names = [runtime.name for runtime in runtimes]
    if len(names) != len(set(names)):
        raise ValueError("Connection plugins registered duplicate runtime names")
    return sorted(runtimes, key=lambda runtime: runtime.name)
