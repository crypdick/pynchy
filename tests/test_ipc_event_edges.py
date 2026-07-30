"""IPC watchdog filtering boundary contracts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from watchdog.events import FileCreatedEvent

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
