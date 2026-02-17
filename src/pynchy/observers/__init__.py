"""Observer system for pynchy event capture.

Observers subscribe to the EventBus and persist or process events.
Built-in observers live under ``observers/plugins/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.event_bus import EventBus


@runtime_checkable
class ObserverProvider(Protocol):
    """Observer provider contract implemented by plugins."""

    name: str

    def subscribe(self, event_bus: EventBus) -> None: ...

    async def close(self) -> None: ...


def _is_valid_observer(candidate: Any) -> bool:
    return all(
        [
            hasattr(candidate, "name"),
            callable(getattr(candidate, "subscribe", None)),
            callable(getattr(candidate, "close", None)),
        ]
    )


def attach_observers(event_bus: EventBus) -> list[ObserverProvider]:
    """Discover observer plugins and subscribe them to the event bus.

    Returns the list of attached observers (for later teardown via close()).
    """
    try:
        from pynchy.plugin import get_plugin_manager
    except Exception:
        logger.exception("Failed to import plugin manager while loading observers")
        return []

    try:
        pm = get_plugin_manager()
        provided = pm.hook.pynchy_observer()
    except Exception:
        logger.exception("Failed to resolve observer plugins")
        return []

    observers: list[ObserverProvider] = []
    for obs in provided:
        if obs is None:
            continue
        if not _is_valid_observer(obs):
            logger.warning(
                "Ignoring invalid observer plugin object",
                observer_type=type(obs).__name__,
            )
            continue
        try:
            obs.subscribe(event_bus)
            observers.append(obs)
            logger.info("Attached observer", name=obs.name)
        except Exception:
            logger.exception("Failed to attach observer", name=getattr(obs, "name", "?"))

    return observers
