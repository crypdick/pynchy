"""Ownership claims and failed-file cleanup for queued IPC files."""

import contextlib
from pathlib import Path

from pynchy.logger import logger

_inflight_ipc_files: set[Path] = set()


def claim_ipc_file(file_path: Path) -> bool:
    """Claim a queued file before processing it through one watcher path.

    Watchdog delivery and the five-second recovery sweep can observe the same
    file. The claim keeps the second observer from replaying it. A missing
    file was already handled by the other observer.
    """
    if not file_path.exists() or file_path in _inflight_ipc_files:
        return False
    _inflight_ipc_files.add(file_path)
    return True


def release_ipc_file(file_path: Path) -> None:
    """Release a completed IPC-file claim."""
    _inflight_ipc_files.discard(file_path)


def move_failed_ipc_file(ipc_base_dir: Path, source_group: str, file_path: Path) -> None:
    """Quarantine a failed file without masking the error being handled."""
    try:
        error_dir = ipc_base_dir / "errors"
        error_dir.mkdir(parents=True, exist_ok=True)
        file_path.rename(error_dir / f"{source_group}-{file_path.name}")
    except OSError:
        logger.warning(
            "Failed to move IPC file to error dir, deleting instead",
            file=file_path.name,
            source_group=source_group,
        )
        with contextlib.suppress(OSError):
            file_path.unlink()
