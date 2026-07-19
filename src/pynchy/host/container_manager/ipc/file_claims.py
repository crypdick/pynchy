"""In-process ownership claims for queued IPC files."""

from pathlib import Path

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
