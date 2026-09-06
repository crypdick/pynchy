"""IPC file writing — atomic message and signal delivery to containers.

Provides the write side of IPC: delivering messages and control signals
to running containers via their input directory.  The read side (processing
output from containers) lives in :mod:`_watcher`.

All writes use atomic rename (tmp → final) so the container's watchdog
never sees a partially-written file.
"""

from __future__ import annotations

import contextlib
import secrets
import time
from pathlib import Path  # noqa: TC003 - beartype resolves IPC write signatures at runtime.
from typing import Any

from pynchy.atomic_json import write_json_atomic, write_text_atomic
from pynchy.host.container_manager.ipc.protocol import validate_request_id

_ipc_base_dir: Path | None = None


def configure_ipc_base_dir(path: Path) -> None:
    """Set the host-owned IPC root during application composition."""
    global _ipc_base_dir  # noqa: PLW0603 - one host process owns one IPC root.
    _ipc_base_dir = path


def _configured_ipc_base_dir() -> Path:
    if _ipc_base_dir is None:
        raise RuntimeError("IPC base directory has not been configured")
    return _ipc_base_dir


def _ipc_input_dir(group_folder: str) -> Path:
    """Return the IPC input directory for a group, creating it if needed."""
    d = _configured_ipc_base_dir() / group_folder / "input"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_ipc_message(
    group_folder: str,
    text: str,
    *,
    turn_id: str | None = None,
    query_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Write a JSON message file to a group's IPC input directory.

    Uses atomic write (tmp → rename) so the container's file watcher
    never sees a partially-written file.
    """
    input_dir = _ipc_input_dir(group_folder)
    filename = f"{int(time.time() * 1000)}-{secrets.token_hex(3)}.json"
    payload: dict[str, object] = {"type": "message", "text": text}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    if query_id is not None:
        payload["query_id"] = query_id
    if metadata:
        payload["metadata"] = metadata
    write_json_atomic(input_dir / filename, payload)


def write_ipc_close_sentinel(group_folder: str) -> None:
    """Write the ``_close`` sentinel to signal a container to wind down."""
    input_dir = _ipc_input_dir(group_folder)
    write_text_atomic(input_dir / "_close", "")


def ipc_response_path(source_group: str, request_id: str) -> Path:
    """Build the IPC response file path for a group request.

    Single source of truth — used by service handlers, approval handlers,
    and the approval sweep.
    """
    safe_request_id = validate_request_id(request_id)
    return _configured_ipc_base_dir() / source_group / "responses" / f"{safe_request_id}.json"


def write_ipc_response(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON response file atomically (tmp → rename).

    Used by IPC handlers to write responses that containers pick up
    (e.g. merge results, service request responses).
    """
    write_json_atomic(path, data)


def clean_ipc_input_dir(group_folder: str | None) -> None:
    """Remove stale IPC input before spawning or after stopping the worker."""
    if not group_folder:
        return
    input_dir = _configured_ipc_base_dir() / group_folder / "input"
    if not input_dir.is_dir():
        return
    for f in input_dir.iterdir():
        with contextlib.suppress(OSError):
            f.unlink()


def clean_secret_files(group_folder: str | None) -> None:
    """Remove host-written secret payloads after their agent runtime exits."""
    if not group_folder:
        return
    secrets_dir = _configured_ipc_base_dir() / group_folder / "secrets"
    if not secrets_dir.is_dir():
        return
    for path in secrets_dir.iterdir():
        with contextlib.suppress(OSError):
            path.unlink()
