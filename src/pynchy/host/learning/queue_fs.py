"""Filesystem primitives for the Obsidian learning queue."""

from __future__ import annotations

import fcntl
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from pynchy.host.learning.queue_models import LearningQueueError

PENDING = "pending"
CLAIMING = "claiming"
CLAIMED = "claimed"
DONE = "done"
ERRORS = "errors"

_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.Lock] = {}


@dataclass(frozen=True)
class QueueLayout:
    base_dir: Path

    @property
    def pending_dir(self) -> Path:
        return self.base_dir / PENDING

    @property
    def claiming_dir(self) -> Path:
        return self.base_dir / CLAIMING

    @property
    def claimed_dir(self) -> Path:
        return self.base_dir / CLAIMED

    @property
    def done_dir(self) -> Path:
        return self.base_dir / DONE

    @property
    def errors_dir(self) -> Path:
        return self.base_dir / ERRORS

    @property
    def state_dirs(self) -> tuple[Path, ...]:
        return (
            self.pending_dir,
            self.claiming_dir,
            self.claimed_dir,
            self.done_dir,
            self.errors_dir,
        )

    @property
    def active_dirs(self) -> tuple[Path, ...]:
        return (self.pending_dir, self.claiming_dir, self.claimed_dir)

    @property
    def terminal_dirs(self) -> tuple[Path, ...]:
        return (self.done_dir, self.errors_dir)


def ensure_state_dirs(layout: QueueLayout) -> None:
    for directory in layout.state_dirs:
        directory.mkdir(parents=True, exist_ok=True)


def discard_duplicates_with_terminal_winner(layout: QueueLayout) -> int:
    """Restart-safe queue recovery; power-loss atomicity requires directory fsync."""
    discarded = 0
    terminal_names: set[str] = set()
    for directory in layout.terminal_dirs:
        for terminal_path in sorted(directory.glob("*.json")):
            if terminal_path.name in terminal_names:
                terminal_path.unlink(missing_ok=True)
                discarded += 1
                continue
            terminal_names.add(terminal_path.name)

    if not terminal_names:
        return discarded

    for directory in layout.active_dirs:
        for active_path in sorted(directory.glob("*.json")):
            if active_path.name in terminal_names:
                active_path.unlink(missing_ok=True)
                discarded += 1
    return discarded


@contextmanager
def transition_lock(base_dir: Path) -> Iterator[None]:
    lock_path = base_dir / ".queue.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _process_lock_for(lock_path)
    with process_lock, lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def rename_no_clobber(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise LearningQueueError(f"queue destination already exists: {destination}") from exc
    try:
        source.unlink()
    except OSError:
        with suppress(FileNotFoundError):
            destination.unlink()
        raise


def _process_lock_for(lock_path: Path) -> threading.Lock:
    key = lock_path.absolute()
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.get(key)
        if process_lock is None:
            process_lock = threading.Lock()
            _PROCESS_LOCKS[key] = process_lock
        return process_lock
