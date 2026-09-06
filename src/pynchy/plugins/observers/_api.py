"""Observer system for pynchy event capture.

Observers subscribe to the EventBus and persist or process events.
Built-in observers live under ``plugins/observers/``.
"""

from __future__ import annotations

from typing import Protocol, TypeGuard, runtime_checkable

import pluggy

from pynchy.event_bus import (
    EventBus,
)
from pynchy.logger import logger

__all__ = ["ObserverProvider", "attach_observers"]


@runtime_checkable
class ObserverProvider(Protocol):
    """Observer provider contract implemented by plugins."""

    name: str

    def subscribe(self, event_bus: EventBus) -> None: ...

    async def close(self) -> None: ...


def _is_valid_observer(candidate: object) -> TypeGuard[ObserverProvider]:
    return all(
        [
            hasattr(candidate, "name"),
            callable(getattr(candidate, "subscribe", None)),
            callable(getattr(candidate, "close", None)),
        ]
    )


def attach_observers(
    plugin_manager: pluggy.PluginManager,
    event_bus: EventBus,
) -> list[ObserverProvider]:
    """Discover observer plugins and subscribe them to the event bus.

    Returns the list of attached observers (for later teardown via close()).
    """
    try:
        candidates = plugin_manager.hook.pynchy_observer()
    except Exception:  # noqa: BLE001 - one plugin must not break observer discovery.
        logger.exception("Failed to resolve observer plugins")
        return []
    observers = [candidate for candidate in candidates if _is_valid_observer(candidate)]

    attached: list[ObserverProvider] = []
    for obs in observers:
        try:
            obs.subscribe(event_bus)
            attached.append(obs)
            logger.info("Attached observer", name=obs.name)
        except Exception:  # noqa: BLE001 - observer plugins are isolated best-effort extensions.
            logger.exception("Failed to attach observer", name=getattr(obs, "name", "?"))

    return attached
