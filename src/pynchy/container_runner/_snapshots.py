"""IPC snapshot helpers — written before container launch for agent to read."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pynchy.config import get_settings


def write_tasks_snapshot(
    folder: str,
    is_admin: bool,
    tasks: list[dict[str, Any]],
    *,
    host_jobs: list[dict[str, Any]] | None = None,
) -> None:
    """Write current_tasks.json to the group's IPC directory.

    Combines agent tasks and host jobs into a single snapshot list.
    Admin groups see everything; non-admin groups see only their own tasks.
    """
    group_ipc_dir = get_settings().data_dir / "ipc" / folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # Admin sees all tasks, others only see their own
    filtered = tasks if is_admin else [t for t in tasks if t.get("groupFolder") == folder]

    # Host jobs are admin-only; append when present
    if host_jobs and is_admin:
        filtered = [*filtered, *host_jobs]

    (group_ipc_dir / "current_tasks.json").write_text(json.dumps(filtered, indent=2))


def write_groups_snapshot(
    folder: str,
    is_admin: bool,
    groups: list[dict[str, Any]],
    registered_jids: set[str],
) -> None:
    """Write available_groups.json to the group's IPC directory."""
    group_ipc_dir = get_settings().data_dir / "ipc" / folder
    group_ipc_dir.mkdir(parents=True, exist_ok=True)

    # Admin sees all groups; others see nothing (they can't activate groups)
    visible = groups if is_admin else []
    payload = {
        "groups": visible,
        "lastSync": datetime.now(UTC).isoformat(),
    }
    (group_ipc_dir / "available_groups.json").write_text(json.dumps(payload, indent=2))
