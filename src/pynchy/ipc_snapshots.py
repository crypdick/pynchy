"""IPC snapshot helpers — written before container launch for agent to read.

Uses atomic writes (tmp → rename) because these files are mounted into
containers that may read them at any time during warm-path queries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pynchy.atomic_json import write_json_atomic


def write_tasks_snapshot(
    data_dir: Path,
    folder: str,
    tasks: list[dict[str, Any]],
    *,
    is_admin: bool,
    host_jobs: list[dict[str, Any]] | None = None,
) -> None:
    """Write current_tasks.json to the group's IPC directory.

    Combines agent tasks and host jobs into a single snapshot list.
    Admin groups see everything; non-admin groups see only their own tasks.
    """
    # Admin sees all tasks, others only see their own
    filtered = tasks if is_admin else [t for t in tasks if t.get("groupFolder") == folder]

    # Host jobs are admin-only; append when present
    if host_jobs and is_admin:
        filtered = [*filtered, *host_jobs]

    path = data_dir / "ipc" / folder / "current_tasks.json"
    write_json_atomic(path, filtered, indent=2)


def write_groups_snapshot(
    data_dir: Path,
    folder: str,
    groups: list[dict[str, Any]],
    _registered_jids: set[str],
    *,
    is_admin: bool,
) -> None:
    """Write available_groups.json to the group's IPC directory."""
    # Admin sees all groups; others see nothing (they can't activate groups)
    visible = groups if is_admin else []
    payload = {
        "groups": visible,
        "lastSync": datetime.now(UTC).isoformat(),
    }

    path = data_dir / "ipc" / folder / "available_groups.json"
    write_json_atomic(path, payload, indent=2)
