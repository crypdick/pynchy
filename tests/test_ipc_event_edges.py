"""IPC watchdog filtering boundary contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from watchdog.events import FileCreatedEvent, FileMovedEvent

from pynchy.host.container_manager.ipc.events import IpcEventHandler

if TYPE_CHECKING:
    from pathlib import Path


def test_ipc_event_outside_root_is_ignored(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    try:
        queue: asyncio.Queue[Path] = asyncio.Queue()
        handler = IpcEventHandler(tmp_path / "ipc", loop, queue)

        handler.on_created(FileCreatedEvent(str(tmp_path / "outside.json")))

        assert queue.empty()
    finally:
        loop.close()


def test_ipc_event_atomic_moves_queue_json_destinations(tmp_path: Path) -> None:
    loop = asyncio.new_event_loop()
    try:
        queue: asyncio.Queue[Path] = asyncio.Queue()
        ipc_dir = tmp_path / "ipc"
        handler = IpcEventHandler(ipc_dir, loop, queue)

        handler.on_moved(
            FileMovedEvent(
                str(ipc_dir / "group" / "requests" / "pending.tmp"),
                str(ipc_dir / "group" / "requests" / "request.json"),
            )
        )
        handler.on_moved(
            FileMovedEvent(
                str(ipc_dir / "group" / "requests" / "pending.tmp"),
                str(ipc_dir / "group" / "requests" / "pending.txt"),
            )
        )
        handler.on_moved(object())
        handler.on_created(object())
        loop.run_until_complete(asyncio.sleep(0))

        assert queue.get_nowait() == ipc_dir / "group" / "requests" / "request.json"
        assert queue.empty()
    finally:
        loop.close()
