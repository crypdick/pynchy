"""File-based IPC watcher.

Async polling loop that processes IPC files from containers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from pynchy.config import get_settings
from pynchy.ipc._deps import IpcDeps
from pynchy.ipc._registry import dispatch
from pynchy.logger import logger

_ipc_watcher_running = False


def _move_to_error_dir(ipc_base_dir: Path, source_group: str, file_path: Path) -> None:
    """Move a failed IPC file to the errors/ directory for later inspection."""
    error_dir = ipc_base_dir / "errors"
    error_dir.mkdir(parents=True, exist_ok=True)
    file_path.rename(error_dir / f"{source_group}-{file_path.name}")


async def start_ipc_watcher(deps: IpcDeps) -> None:
    """Start the IPC watcher polling loop."""
    global _ipc_watcher_running
    if _ipc_watcher_running:
        logger.debug("IPC watcher already running, skipping duplicate start")
        return
    _ipc_watcher_running = True

    s = get_settings()
    ipc_base_dir = s.data_dir / "ipc"
    ipc_base_dir.mkdir(parents=True, exist_ok=True)

    async def process_ipc_files() -> None:
        try:
            group_folders = [
                f.name for f in ipc_base_dir.iterdir() if f.is_dir() and f.name != "errors"
            ]
        except OSError as exc:
            logger.error("Error reading IPC base directory", err=str(exc))
            await asyncio.sleep(s.intervals.ipc_poll)
            return

        registered_groups = deps.registered_groups()
        _god_folders = {g.folder for g in registered_groups.values() if g.is_god}

        for source_group in group_folders:
            is_god = source_group in _god_folders
            messages_dir = ipc_base_dir / source_group / "messages"
            tasks_dir = ipc_base_dir / source_group / "tasks"

            # Process messages
            try:
                if messages_dir.exists():
                    message_files = sorted(f for f in messages_dir.iterdir() if f.suffix == ".json")
                    for file_path in message_files:
                        try:
                            data = json.loads(file_path.read_text())
                            if (
                                data.get("type") == "message"
                                and data.get("chatJid")
                                and data.get("text")
                            ):
                                target_group = registered_groups.get(data["chatJid"])
                                if is_god or (target_group and target_group.folder == source_group):
                                    await deps.broadcast_to_channels(
                                        data["chatJid"],
                                        f"{s.agent.name}: {data['text']}",
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
            except OSError as exc:
                logger.error(
                    "Error reading IPC messages directory",
                    err=str(exc),
                    source_group=source_group,
                )

            # Process tasks
            try:
                if tasks_dir.exists():
                    task_files = sorted(f for f in tasks_dir.iterdir() if f.suffix == ".json")
                    for file_path in task_files:
                        try:
                            data = json.loads(file_path.read_text())
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
            except OSError as exc:
                logger.error(
                    "Error reading IPC tasks directory",
                    err=str(exc),
                    source_group=source_group,
                )

    while True:
        await process_ipc_files()
        await asyncio.sleep(s.intervals.ipc_poll)
