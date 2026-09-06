"""Process inbound IPC message and request files."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,  # beartype resolves IPC deps at runtime.
    IpcMessageDeps,
)
from pynchy.host.container_manager.ipc.protocol import InboundChatMessage, parse_ipc_file
from pynchy.logger import logger
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)


def _path_exists(path: Path) -> bool:
    return path.exists()


@dataclass(frozen=True)
class QueuedIpcFile:
    path: Path
    source_group: str
    subdir: str
    is_admin: bool


def _unlink_path(path: Path) -> None:
    path.unlink()


def _default_agent_name(deps: IpcDeps) -> str:
    if not isinstance(deps, IpcMessageDeps):
        raise TypeError("Inbound IPC messages require IpcMessageDeps")
    return deps.default_agent_name()


async def handle_message_file(
    file_path: Path,
    source_group: str,
    *,
    is_admin: bool,
    deps: IpcDeps,
) -> None:
    message = InboundChatMessage.from_dict(parse_ipc_file(file_path))

    if message is not None:
        workspaces = deps.workspaces()
        target_group = workspaces.get(message.chat_jid)
        if is_admin or (target_group and target_group.folder == source_group):
            prefix = message.sender or _default_agent_name(deps)
            await deps.broadcast_to_channels(
                message.chat_jid,
                OutboundEvent(
                    type=OutboundEventType.TEXT,
                    content=f"{prefix}: {message.text}",
                ),
            )
            logger.info(
                "IPC message sent",
                chat_jid=message.chat_jid,
                source_group=source_group,
            )
        else:
            logger.warning(
                "Unauthorized IPC message attempt blocked",
                chat_jid=message.chat_jid,
                source_group=source_group,
            )
    await asyncio.to_thread(_unlink_path, file_path)


async def classify_queued_ipc_file(
    file_path: Path, ipc_base_dir: Path, deps: IpcDeps
) -> QueuedIpcFile | None:
    if not await asyncio.to_thread(_path_exists, file_path):
        return None

    relative = file_path.relative_to(ipc_base_dir)
    parts = relative.parts
    source_group = parts[0]
    subdir = parts[1]

    current_groups = deps.workspaces()
    current_admin_folders = {g.folder for g in current_groups.values() if g.is_admin}
    is_admin = source_group in current_admin_folders
    return QueuedIpcFile(
        path=file_path,
        source_group=source_group,
        subdir=subdir,
        is_admin=is_admin,
    )
