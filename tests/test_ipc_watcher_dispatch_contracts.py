"""Behavioral coverage for the watchdog queue and runtime sweep loop."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from conftest_helpers import NullIpcDeps

from pynchy.host.container_manager.ipc.watcher import start_ipc_watcher


@dataclass
class _State:
    running: bool = False
    runtime_sweep_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class _Queued:
    path: Path
    source_group: str
    subdir: str
    is_admin: bool = False


class _Observer:
    daemon = False

    def schedule(self, *_args, **_kwargs):
        return None

    def start(self):
        return None


class _SeedHandler:
    def __init__(self, _base: Path, _loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Path]):
        queue.put_nowait(Path("queued.json"))


@pytest.mark.parametrize(
    ("subdir", "processor_name"),
    [
        ("messages", "process_ipc_message_file"),
        ("requests", "process_ipc_request_file"),
        ("output", "process_output_file"),
    ],
)
async def test_start_dispatches_each_queued_ipc_subdirectory(subdir: str, processor_name: str):
    state = _State()
    queued = _Queued(Path("queued.json"), "group", subdir)
    processor = AsyncMock(side_effect=asyncio.CancelledError)

    with (
        patch("pynchy.host.container_manager.ipc.watcher._state", state),
        patch("pynchy.host.container_manager.ipc.watcher.Observer", return_value=_Observer()),
        patch("pynchy.host.container_manager.ipc.watcher.IpcEventHandler", _SeedHandler),
        patch(
            "pynchy.host.container_manager.ipc.watcher.recover_ipc_startup",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher._runtime_sweep_loop",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.classify_queued_ipc_file",
            new_callable=AsyncMock,
            return_value=queued,
        ),
        patch(
            f"pynchy.host.container_manager.ipc.watcher.{processor_name}",
            processor,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await start_ipc_watcher(NullIpcDeps(), ipc_base_dir=Path("ipc"))

    processor.assert_awaited_once()


@pytest.mark.parametrize(
    "first_result",
    [None, _Queued(Path("queued.json"), "group", "unknown")],
)
async def test_start_ignores_unclassifiable_or_unknown_queued_items(first_result):
    state = _State()
    classify = AsyncMock(side_effect=[first_result, asyncio.CancelledError])

    class _TwoSeedHandler:
        def __init__(
            self,
            _base: Path,
            _loop: asyncio.AbstractEventLoop,
            queue: asyncio.Queue[Path],
        ):
            queue.put_nowait(Path("first.json"))
            queue.put_nowait(Path("second.json"))

    with (
        patch("pynchy.host.container_manager.ipc.watcher._state", state),
        patch("pynchy.host.container_manager.ipc.watcher.Observer", return_value=_Observer()),
        patch("pynchy.host.container_manager.ipc.watcher.IpcEventHandler", _TwoSeedHandler),
        patch(
            "pynchy.host.container_manager.ipc.watcher.recover_ipc_startup",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher._runtime_sweep_loop",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.classify_queued_ipc_file",
            classify,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await start_ipc_watcher(NullIpcDeps(), ipc_base_dir=Path("ipc"))

    assert classify.await_count == 2


async def test_start_scopes_one_queued_file_error_and_continues():
    state = _State()
    classify = AsyncMock(side_effect=[RuntimeError("bad file"), asyncio.CancelledError])

    class _TwoSeedHandler:
        def __init__(
            self,
            _base: Path,
            _loop: asyncio.AbstractEventLoop,
            queue: asyncio.Queue[Path],
        ):
            queue.put_nowait(Path("first.json"))
            queue.put_nowait(Path("second.json"))

    with (
        patch("pynchy.host.container_manager.ipc.watcher._state", state),
        patch("pynchy.host.container_manager.ipc.watcher.Observer", return_value=_Observer()),
        patch("pynchy.host.container_manager.ipc.watcher.IpcEventHandler", _TwoSeedHandler),
        patch(
            "pynchy.host.container_manager.ipc.watcher.recover_ipc_startup",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher._runtime_sweep_loop",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.classify_queued_ipc_file",
            classify,
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await start_ipc_watcher(NullIpcDeps(), ipc_base_dir=Path("ipc"))

    assert classify.await_count == 2


async def test_start_runs_one_runtime_recovery_sweep():
    state = _State()

    async def stop_after_sweep(_queue, _base, _deps):
        for _ in range(4):
            await asyncio.sleep(0)
        task = state.runtime_sweep_task
        assert task is not None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    with (
        patch("pynchy.host.container_manager.ipc.watcher._state", state),
        patch("pynchy.host.container_manager.ipc.watcher.Observer", return_value=_Observer()),
        patch("pynchy.host.container_manager.ipc.watcher.IpcEventHandler", _SeedHandler),
        patch(
            "pynchy.host.container_manager.ipc.watcher.recover_ipc_startup",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "pynchy.host.container_manager.ipc.watcher.recover_ipc_runtime",
            new_callable=AsyncMock,
            side_effect=[1, 0, 0, 0],
        ) as recover,
        patch("pynchy.host.container_manager.ipc.watcher.IPC_RUNTIME_SWEEP_INTERVAL_SECONDS", 0),
        patch("pynchy.host.container_manager.ipc.watcher._process_queue", stop_after_sweep),
    ):
        await start_ipc_watcher(NullIpcDeps(), ipc_base_dir=Path("ipc"))

    assert recover.await_count >= 2
