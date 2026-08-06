"""Cross-process behavior for the Apple Container builder lock."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - regression test starts a controlled lock holder.
import sys
import time
from typing import TYPE_CHECKING

import pytest

from pynchy.plugins.runtimes.apple_build_lock import apple_build_lock

if TYPE_CHECKING:
    from pathlib import Path


def test_lock_rejects_a_second_process_then_releases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lock_path = tmp_path / "apple-build.lock"
    monkeypatch.setenv("PYNCHY_APPLE_BUILD_LOCK_PATH", str(lock_path))
    environment = {**os.environ, "PYNCHY_APPLE_BUILD_LOCK_PATH": str(lock_path)}
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from pynchy.plugins.runtimes.apple_build_lock import apple_build_lock; "
                "import time; lock = apple_build_lock(); lock.__enter__(); time.sleep(1)"
            ),
        ],
        env=environment,
    )
    try:
        deadline = time.monotonic() + 5
        while not lock_path.exists() or not lock_path.read_text(encoding="utf-8"):
            if time.monotonic() >= deadline:
                pytest.fail("lock holder did not acquire the lock")
            time.sleep(0.01)
        with (
            pytest.raises(TimeoutError, match="build lock busy"),
            apple_build_lock(timeout=0.1),
        ):
            pass
    finally:
        holder.wait(timeout=5)

    with apple_build_lock(timeout=0.1):
        pass
