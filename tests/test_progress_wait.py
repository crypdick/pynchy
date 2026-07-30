"""Public completion and timeout contracts for progress-aware waits."""

from __future__ import annotations

import asyncio

import pytest

from pynchy.progress_wait import ProgressTimeoutError, wait_for_progress


async def test_wait_returns_completed_operation_after_consuming_progress() -> None:
    progress = asyncio.Event()
    progress.set()

    async def operation() -> str:
        await asyncio.sleep(0)
        return "done"

    assert (
        await wait_for_progress(
            operation(),
            progress_event=progress,
            inactivity_timeout_seconds=1,
        )
        == "done"
    )
    assert not progress.is_set()


async def test_wait_returns_an_already_completed_task() -> None:
    task = asyncio.create_task(asyncio.sleep(0, result="done"))
    await task

    assert (
        await wait_for_progress(
            task,
            progress_event=asyncio.Event(),
            inactivity_timeout_seconds=1,
        )
        == "done"
    )


async def test_progress_arriving_during_wait_refreshes_the_deadline() -> None:
    progress = asyncio.Event()

    async def operation() -> str:
        progress.set()
        await asyncio.sleep(0.01)
        return "done"

    assert (
        await wait_for_progress(
            operation(),
            progress_event=progress,
            inactivity_timeout_seconds=1,
        )
        == "done"
    )


async def test_wait_times_out_when_initial_progress_never_arrives() -> None:
    with pytest.raises(ProgressTimeoutError, match="initial_progress timeout") as exc_info:
        await wait_for_progress(
            asyncio.Event().wait(),
            progress_event=asyncio.Event(),
            inactivity_timeout_seconds=1,
            initial_progress_timeout_seconds=0.01,
            hard_timeout_seconds=2,
        )

    assert exc_info.value.reason == "initial_progress"


async def test_zero_inactivity_budget_times_out_without_starting_work() -> None:
    with pytest.raises(ProgressTimeoutError, match="inactivity timeout"):
        await wait_for_progress(
            asyncio.Event().wait(),
            progress_event=asyncio.Event(),
            inactivity_timeout_seconds=0,
            hard_timeout_seconds=1,
        )


async def test_wait_cancels_stalled_operation_on_inactivity_timeout() -> None:
    cancelled = asyncio.Event()

    async def operation() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(ProgressTimeoutError, match="inactivity timeout") as exc_info:
        await wait_for_progress(
            operation(),
            progress_event=asyncio.Event(),
            inactivity_timeout_seconds=0.01,
            hard_timeout_seconds=1,
        )

    assert exc_info.value.reason == "inactivity"
    assert cancelled.is_set()


async def test_progress_signal_wins_wait_timeout_race(monkeypatch) -> None:
    progress = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def operation() -> str:
        await release.wait()
        return "done"

    async def fake_wait(tasks, **_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        if calls == 1:
            progress.set()
            return set(), set()
        release.set()
        return {tasks[0]}, set()

    monkeypatch.setattr("pynchy.progress_wait.asyncio.wait", fake_wait)

    assert (
        await wait_for_progress(
            operation(),
            progress_event=progress,
            inactivity_timeout_seconds=1,
        )
        == "done"
    )
