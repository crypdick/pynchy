"""Signal-style IPC request handlers."""

from __future__ import annotations

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.logger import logger


async def handle_signal(
    signal_type: str,
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - signal dispatcher is invoked positionally from the watcher.
    deps: IpcDeps,
) -> None:
    """Handle a payload-free IPC request whose behavior is host-derived."""
    if signal_type == "refresh_groups":
        if is_admin:
            logger.info(
                "Group metadata refresh requested via signal",
                source_group=source_group,
            )
            workspaces = deps.workspaces()
            await deps.sync_group_metadata(force=True)
            available_groups = await deps.get_available_groups()
            deps.write_groups_snapshot(
                group_folder=source_group,
                is_admin=True,
                available_groups=available_groups,
                registered_jids=set(workspaces.keys()),
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
