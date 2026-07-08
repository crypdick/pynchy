"""Temporal-backed orchestrator components."""

__all__ = ["TemporalSchedulerRuntime"]


def __getattr__(name: str):
    if name == "TemporalSchedulerRuntime":
        from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

        return TemporalSchedulerRuntime
    raise AttributeError(name)
