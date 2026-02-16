"""File-based IPC watcher.

Uses watchdog (inotify on Linux, FSEvents on macOS) for event-driven
file processing.  On startup, sweeps existing files for crash recovery.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pynchy.config import get_settings
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._protocol import parse_ipc_file, validate_signal
from pynchy.ipc._registry import dispatch
from pynchy.logger import logger

_ipc_watcher_lock = asyncio.Lock()
_ipc_watcher_running = False


def _move_to_error_dir(ipc_base_dir: Path, source_group: str, file_path: Path) -> None:
    """Move a failed IPC file to the errors/ directory for later inspection."""
    error_dir = ipc_base_dir / "errors"
    error_dir.mkdir(parents=True, exist_ok=True)
    file_path.rename(error_dir / f"{source_group}-{file_path.name}")


async def _process_message_file(
    file_path: Path,
    source_group: str,
    is_god: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Process a single IPC message file."""
    s = get_settings()
    try:
        data = parse_ipc_file(file_path)

        if data.get("type") == "message" and data.get("chatJid") and data.get("text"):
            registered_groups = deps.registered_groups()
            target_group = registered_groups.get(data["chatJid"])
            if is_god or (target_group and target_group.folder == source_group):
                sender = data.get("sender")
                prefix = f"{sender}" if sender else s.agent.name
                await deps.broadcast_to_channels(
                    data["chatJid"],
                    f"{prefix}: {data['text']}",
                )
                logger.info(
                    "IPC message sent",
                    chat_jid=data["chatJid"],
                    source_group=source_group,
                )
            else:
                logger.warning(
                    "Unauthorized IPC message attempt blocked",
                    chat_jid=data["chatJid"],
                    source_group=source_group,
                )
        file_path.unlink()
    except Exception as exc:
        logger.error(
            "Error processing IPC message",
            file=file_path.name,
            source_group=source_group,
            err=str(exc),
        )
        _move_to_error_dir(ipc_base_dir, source_group, file_path)


async def _process_task_file(
    file_path: Path,
    source_group: str,
    is_god: bool,
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Process a single IPC task file.

    Routes Tier 1 signals to _handle_signal, Tier 2 requests to dispatch.
    """
    try:
        data = parse_ipc_file(file_path)

        # Tier 1: signal-only
        signal_type = validate_signal(data)
        if signal_type is not None:
            await _handle_signal(signal_type, source_group, is_god, deps)
            file_path.unlink()
            return

        # Tier 2: data-carrying request
        await dispatch(data, source_group, is_god, deps)
        file_path.unlink()
    except Exception as exc:
        logger.error(
            "Error processing IPC task",
            file=file_path.name,
            source_group=source_group,
            err=str(exc),
        )
        _move_to_error_dir(ipc_base_dir, source_group, file_path)


async def _handle_signal(
    signal_type: str,
    source_group: str,
    is_god: bool,
    deps: IpcDeps,
) -> None:
    """Handle a Tier 1 signal-only IPC request.

    Signals carry no payload — the host derives behavior from the signal
    type and its own state (which group sent it, registered groups, etc.).
    """
    if signal_type == "refresh_groups":
        if is_god:
            logger.info(
                "Group metadata refresh requested via signal",
                source_group=source_group,
            )
            registered_groups = deps.registered_groups()
            await deps.sync_group_metadata(True)
            available_groups = await deps.get_available_groups()
            deps.write_groups_snapshot(
                source_group,
                True,
                available_groups,
                set(registered_groups.keys()),
            )
        else:
            logger.warning(
                "Unauthorized refresh_groups signal blocked",
                source_group=source_group,
            )
    else:
        logger.warning(
            "Unknown signal type",
            signal=signal_type,
            source_group=source_group,
        )


async def _sweep_directory(
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> int:
    """Process all existing IPC files (crash recovery).

    Returns the number of files processed.
    """
    processed = 0
    try:
        group_folders = [
            f.name for f in ipc_base_dir.iterdir() if f.is_dir() and f.name != "errors"
        ]
    except OSError as exc:
        logger.error("Error reading IPC base directory during sweep", err=str(exc))
        return 0

    registered_groups = deps.registered_groups()
    god_folders = {g.folder for g in registered_groups.values() if g.is_god}

    for source_group in group_folders:
        is_god = source_group in god_folders
        messages_dir = ipc_base_dir / source_group / "messages"
        tasks_dir = ipc_base_dir / source_group / "tasks"

        # Process messages
        try:
            if messages_dir.exists():
                for file_path in sorted(f for f in messages_dir.iterdir() if f.suffix == ".json"):
                    await _process_message_file(file_path, source_group, is_god, ipc_base_dir, deps)
                    processed += 1
        except OSError as exc:
            logger.error(
                "Error reading IPC messages directory during sweep",
                err=str(exc),
                source_group=source_group,
            )

        # Process tasks
        try:
            if tasks_dir.exists():
                for file_path in sorted(f for f in tasks_dir.iterdir() if f.suffix == ".json"):
                    await _process_task_file(file_path, source_group, is_god, ipc_base_dir, deps)
                    processed += 1
        except OSError as exc:
            logger.error(
                "Error reading IPC tasks directory during sweep",
                err=str(exc),
                source_group=source_group,
            )

    return processed


class _IpcEventHandler(FileSystemEventHandler):
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
            # Expected: <group>/<messages|tasks>/<file>.json
            if len(parts) == 3 and parts[1] in ("messages", "tasks"):
                self._loop.call_soon_threadsafe(self._queue.put_nowait, file_path)
        except (ValueError, IndexError):
            pass  # File not under IPC base dir or malformed path — ignore

    def on_created(self, event: Any) -> None:
        if isinstance(event, FileCreatedEvent):
            self._enqueue_if_ipc(event.src_path)

    def on_moved(self, event: Any) -> None:
        # Atomic writes (tmp → .json rename) generate moved events, not created
        if isinstance(event, FileMovedEvent):
            self._enqueue_if_ipc(event.dest_path)


async def _process_queue(
    queue: asyncio.Queue[Path],
    ipc_base_dir: Path,
    deps: IpcDeps,
) -> None:
    """Consume the event queue and dispatch IPC files."""
    while True:
        file_path = await queue.get()
        try:
            if not file_path.exists():
                continue

            relative = file_path.relative_to(ipc_base_dir)
            parts = relative.parts
            source_group = parts[0]
            subdir = parts[1]

            # Re-check god status (groups can change at runtime)
            current_groups = deps.registered_groups()
            current_god_folders = {g.folder for g in current_groups.values() if g.is_god}
            is_god = source_group in current_god_folders

            if subdir == "messages":
                await _process_message_file(file_path, source_group, is_god, ipc_base_dir, deps)
            elif subdir == "tasks":
                await _process_task_file(file_path, source_group, is_god, ipc_base_dir, deps)
        except Exception as exc:
            logger.error(
                "Error processing queued IPC file",
                file=str(file_path),
                err=str(exc),
            )
        finally:
            queue.task_done()


async def start_ipc_watcher(deps: IpcDeps) -> None:
    """Start the IPC watcher using watchdog filesystem events.

    1. Performs a startup sweep to process files written while the process was down.
    2. Starts a watchdog Observer for event-driven processing.
    """
    global _ipc_watcher_running
    async with _ipc_watcher_lock:
        if _ipc_watcher_running:
            logger.debug("IPC watcher already running, skipping duplicate start")
            return
        _ipc_watcher_running = True

    s = get_settings()
    ipc_base_dir = s.data_dir / "ipc"
    ipc_base_dir.mkdir(parents=True, exist_ok=True)

    # --- Startup sweep (crash recovery) ---
    swept = await _sweep_directory(ipc_base_dir, deps)
    if swept > 0:
        logger.info("IPC startup sweep processed files", count=swept)

    # --- Start watchdog observer ---
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()

    handler = _IpcEventHandler(ipc_base_dir, loop, queue)
    observer = Observer()
    observer.schedule(handler, str(ipc_base_dir), recursive=True)
    observer.daemon = True
    observer.start()
    logger.info("IPC watcher started (watchdog mode)", path=str(ipc_base_dir))

    await _process_queue(queue, ipc_base_dir, deps)
