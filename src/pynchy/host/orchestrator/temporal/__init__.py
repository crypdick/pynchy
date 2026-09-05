"""Temporal-backed orchestrator components."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime

__all__ = ["TemporalSchedulerRuntime"]


def __getattr__(name: str) -> object:  # noqa: V103
    if name == "TemporalSchedulerRuntime":
        from pynchy.host.orchestrator.temporal.scheduler import (  # noqa: PLC0415 - package attribute stays lazy to avoid importing Temporal scheduler at package import time.
            TemporalSchedulerRuntime,
        )

        return TemporalSchedulerRuntime
    raise AttributeError(name)
