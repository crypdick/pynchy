"""IPC file writing — atomic message and signal delivery to containers.

Provides the write side of IPC: delivering messages and control signals
to running containers via their input directory.  The read side (processing
output from containers) lives in :mod:`_watcher`.

All writes use atomic rename (tmp → final) so the container's watchdog
never sees a partially-written file.
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

from pynchy.config import get_settings


def _ipc_input_dir(group_folder: str) -> Path:
    """Return the IPC input directory for a group, creating it if needed."""
    d = get_settings().data_dir / "ipc" / group_folder / "input"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_ipc_message(group_folder: str, text: str) -> None:
    """Write a JSON message file to a group's IPC input directory.

    Uses atomic write (tmp → rename) so the container's file watcher
    never sees a partially-written file.
    """
    input_dir = _ipc_input_dir(group_folder)
    filename = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}.json"
    filepath = input_dir / filename
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps({"type": "message", "text": text}))
    temp_path.rename(filepath)


def write_ipc_close_sentinel(group_folder: str) -> None:
    """Write the ``_close`` sentinel to signal a container to wind down."""
    input_dir = _ipc_input_dir(group_folder)
    (input_dir / "_close").write_text("")


def ipc_response_path(source_group: str, request_id: str) -> Path:
    """Build the IPC response file path for a group request.

    Single source of truth — used by service handlers, approval handlers,
    and the approval sweep.
    """
    return get_settings().data_dir / "ipc" / source_group / "responses" / f"{request_id}.json"


def write_ipc_response(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON response file atomically (tmp → rename).

    Used by IPC handlers to write responses that containers pick up
    (e.g. merge results, service request responses).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.rename(path)
