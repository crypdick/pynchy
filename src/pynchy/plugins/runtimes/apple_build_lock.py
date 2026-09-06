"""Host-wide advisory lock for Apple Container's shared BuildKit builder."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_LOCK_TIMEOUT_SECONDS = 60
_LOCK_EXIT_CODE = 75
_LOCK_ENV = "PYNCHY_APPLE_BUILD_LOCK_HELD"
_LOCK_PATH_ENV = "PYNCHY_APPLE_BUILD_LOCK_PATH"


def _lock_path() -> Path:
    configured = os.environ.get(_LOCK_PATH_ENV)
    if configured:
        return Path(configured)
    return Path.home() / "Library" / "Application Support" / "Pynchy" / "apple-build.lock"


@contextmanager
def apple_build_lock(*, timeout: float = _LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Hold the host-user-global lock while touching Apple Container build state."""
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Apple Container build lock busy: {path}") from None
                time.sleep(min(0.1, deadline - time.monotonic()))
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(f"pid={os.getpid()} started={time.time():.3f}\n")
            lock_file.flush()
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _exec_locked(command: list[str]) -> int:
    if os.environ.get(_LOCK_ENV) == "1":
        # allow: start-process-with-no-shell - re-exec trusted caller.
        os.execvp(command[0], command)  # noqa: S606
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    sys.stderr.write(f"Apple Container build lock busy: {path}\n")
                    return _LOCK_EXIT_CODE
                time.sleep(min(0.1, deadline - time.monotonic()))
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid={os.getpid()} started={time.time():.3f}\n")
        lock_file.flush()
        inheritable = True
        os.set_inheritable(lock_file.fileno(), inheritable)
        environment = {**os.environ, _LOCK_ENV: "1"}
        # allow: start-process-with-no-shell - re-exec trusted caller.
        os.execvpe(command[0], command, environment)  # noqa: S606
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exec", dest="command", nargs=argparse.REMAINDER, required=True)
    arguments = parser.parse_args()
    if not arguments.command:
        parser.error("--exec requires a command")
    return _exec_locked(arguments.command)


if __name__ == "__main__":
    raise SystemExit(main())
