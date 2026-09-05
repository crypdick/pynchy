"""Watchdog event filtering for IPC files."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler

if TYPE_CHECKING:
    import asyncio


class IpcEventHandler(FileSystemEventHandler):
    """Watchdog handler that enqueues IPC file events for async processing."""

    def __init__(
        self,
        ipc_base_dir: Path,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[Path],
    ) -> None:
        super().__init__()
        self._ipc_base_dir = ipc_base_dir
        self._loop = loop
        self._queue = queue

    def _enqueue_if_ipc(self, path_str: str) -> None:
        """Enqueue a file if it matches the IPC directory structure."""
        if not path_str.endswith(".json"):
            return
        file_path = Path(path_str)
        try:
            relative = file_path.relative_to(self._ipc_base_dir)
            parts = relative.parts
            # Expected: <group>/<messages|requests|output>/<file>.json
            if len(parts) == 3 and parts[1] in ("messages", "requests", "output"):
                self._loop.call_soon_threadsafe(self._queue.put_nowait, file_path)
        except (ValueError, IndexError):
            pass  # File not under IPC base dir or malformed path — ignore

    def on_created(self, event: object) -> None:  # noqa: V105
        if isinstance(event, FileCreatedEvent):
            self._enqueue_if_ipc(os.fsdecode(event.src_path))

    def on_moved(self, event: object) -> None:  # noqa: V105
        # Atomic writes (tmp → .json rename) generate moved events, not created
        if isinstance(event, FileMovedEvent):
            self._enqueue_if_ipc(os.fsdecode(event.dest_path))
