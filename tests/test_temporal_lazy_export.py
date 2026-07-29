"""Public lazy export contract for Temporal scheduler runtime."""

from __future__ import annotations

import pytest

import pynchy.host.orchestrator.temporal as temporal
from pynchy.host.orchestrator.temporal.scheduler import TemporalSchedulerRuntime


def test_temporal_package_lazily_exposes_the_scheduler_runtime() -> None:
    assert temporal.TemporalSchedulerRuntime is TemporalSchedulerRuntime


def test_temporal_package_rejects_unknown_attributes() -> None:
    missing_name = "not_a_temporal_runtime"
    with pytest.raises(AttributeError):
        getattr(temporal, missing_name)
