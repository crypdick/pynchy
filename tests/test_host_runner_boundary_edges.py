"""Boundary tests for stopping direct host agent runners."""

from __future__ import annotations

import os
import signal
from unittest.mock import AsyncMock

import pytest

from pynchy.host.orchestrator.host_runner import stop_host_process
from tests.test_host_runner import _FakeProcess


@pytest.mark.asyncio
async def test_stop_host_process_ignores_a_runner_that_already_exited() -> None:
    fake_proc = _FakeProcess([], returncode=0)

    await stop_host_process(fake_proc)

    assert fake_proc.killed is False


@pytest.mark.asyncio
async def test_stop_host_process_falls_back_to_kill_when_process_group_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_proc = _FakeProcess([], returncode=None)

    def missing_process_group(_pid: int, _signal: signal.Signals) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", missing_process_group)

    await stop_host_process(fake_proc)

    assert fake_proc.killed is True


@pytest.mark.asyncio
async def test_stop_host_process_escalates_after_graceful_stop_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_proc = _FakeProcess([], returncode=None)
    fake_proc.wait = AsyncMock(side_effect=[TimeoutError, 0])  # type: ignore[method-assign]
    signals: list[tuple[int, signal.Signals]] = []

    monkeypatch.setattr(os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    await stop_host_process(fake_proc)

    assert signals == [
        (fake_proc.pid, signal.SIGINT),
        (fake_proc.pid, signal.SIGKILL),
    ]
