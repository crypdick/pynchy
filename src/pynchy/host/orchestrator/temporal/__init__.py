"""Temporal-backed orchestrator components."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

__all__ = ["TemporalSchedulerRuntime"]


def __getattr__(name: str) -> object:
    if name == "TemporalSchedulerRuntime":
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

        return TemporalSchedulerRuntime
    raise AttributeError(name)
