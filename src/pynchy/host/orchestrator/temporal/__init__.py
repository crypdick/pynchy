"""Temporal-backed orchestrator components."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

__all__ = ["TemporalSchedulerRuntime"]


def __getattr__(name: str) -> Any:
    if name == "TemporalSchedulerRuntime":
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

        return TemporalSchedulerRuntime
    raise AttributeError(name)
