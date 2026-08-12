"""Atomic JSON persistence."""

from __future__ import annotations

import json
import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path  # noqa: TC003 - beartype resolves this runtime annotation.


def write_text_atomic(path: Path, payload: str) -> None:
    """Publish text without following shared-directory symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # NOTE: Update docs/architecture/ipc.md "Atomic writes" if this changes.
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    tmp_name = f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            existing_mode = (
                stat.S_IMODE(existing.st_mode) if stat.S_ISREG(existing.st_mode) else None
            )
        except FileNotFoundError:
            existing_mode = None
        tmp_fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o666,
            dir_fd=parent_fd,
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
                if existing_mode is not None:
                    os.fchmod(tmp_file.fileno(), existing_mode)
                tmp_file.write(payload)
            os.replace(tmp_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(tmp_name, dir_fd=parent_fd)
            raise
    finally:
        os.close(parent_fd)


def write_json_atomic(path: Path, data: object, *, indent: int | None = None) -> None:
    """Write JSON data through a temporary file and atomic rename."""
    write_text_atomic(path, json.dumps(data, indent=indent))
