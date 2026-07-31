"""Public startup readiness gate behavior."""

from __future__ import annotations

import pytest

from pynchy.host.orchestrator.startup_readiness import StartupReadiness, StartupReadinessError


@pytest.mark.asyncio
async def test_terminal_startup_signal_is_idempotent() -> None:
    ready = StartupReadiness()
    ready.mark_ready()
    ready.mark_failed(RuntimeError("late failure"))
    await ready.wait()

    failed = StartupReadiness()
    failure = RuntimeError("route recovery failed")
    failed.mark_failed(failure)
    failed.mark_ready()

    with pytest.raises(StartupReadinessError) as error:
        await failed.wait()
    assert error.value.__cause__ is failure
