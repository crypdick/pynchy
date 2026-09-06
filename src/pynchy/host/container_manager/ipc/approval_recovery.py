"""Runtime recovery for host-owned approval decisions."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pynchy.host.container_manager.ipc.deps import (
    IpcDeps,
)
from pynchy.logger import logger


def _json_files_in_dir(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted(file for file in path.iterdir() if file.suffix == ".json")


def _approval_groups(approval_root: Path) -> list[str]:
    return [
        entry.name for entry in approval_root.iterdir() if entry.is_dir() and entry.name != "errors"
    ]


async def _sweep_group_decisions(
    decisions_dir: Path,
    source_group: str,
    deps: IpcDeps,
) -> int:
    try:
        from pynchy.host.container_manager.ipc.handlers_approval import (  # noqa: PLC0415 - keep watcher startup narrow.
            process_approval_decision,
        )

        decision_files = await asyncio.to_thread(_json_files_in_dir, decisions_dir)
        for decision_file in decision_files:
            await process_approval_decision(decision_file, source_group, deps=deps)
    except OSError as exc:
        logger.error(
            "Error reading host approval decisions during runtime sweep",
            err=str(exc),
            source_group=source_group,
        )
        return 0
    return len(decision_files)


async def sweep_host_approval_decisions(deps: IpcDeps) -> int:
    """Recover decisions persisted immediately before a host crash."""
    from pynchy.host.container_manager.security.approval import (  # noqa: PLC0415 - keep watcher startup narrow.
        approval_state_root,
    )

    approval_root = approval_state_root()
    try:
        source_groups = await asyncio.to_thread(_approval_groups, approval_root)
    except OSError as exc:
        logger.error("Error reading host approval state during runtime sweep", err=str(exc))
        return 0

    processed = 0
    for source_group in source_groups:
        processed += await _sweep_group_decisions(
            approval_root / source_group / "approval_decisions",
            source_group,
            deps,
        )
    return processed
