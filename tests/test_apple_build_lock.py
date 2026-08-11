"""Cross-process behavior for the Apple Container builder lock."""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - regression test starts a controlled lock holder.
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.plugins.runtimes.apple_build_lock import apple_build_lock, main


class ExecReplacedError(Exception):
    """Signal that a mocked exec call would have replaced this process."""


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


def test_cli_exec_acquires_default_lock_and_marks_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CLI owns the default lock and passes its held marker to the replacement process."""
    monkeypatch.delenv("PYNCHY_APPLE_BUILD_LOCK_PATH", raising=False)
    monkeypatch.delenv("PYNCHY_APPLE_BUILD_LOCK_HELD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["apple-build-lock", "--exec", "tool", "arg"])

    with patch("pynchy.plugins.runtimes.apple_build_lock.os.execvpe") as execvpe:
        assert main() == 1

    command, argv, environment = execvpe.call_args.args
    assert (command, argv) == ("tool", ["tool", "arg"])
    assert environment["PYNCHY_APPLE_BUILD_LOCK_HELD"] == "1"
    assert (tmp_path / "Library/Application Support/Pynchy/apple-build.lock").exists()


def test_cli_exec_skips_lock_when_child_already_holds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYNCHY_APPLE_BUILD_LOCK_HELD", "1")
    monkeypatch.setattr(sys, "argv", ["apple-build-lock", "--exec", "tool", "arg"])

    with (
        patch.dict(os.environ, {"PYNCHY_APPLE_BUILD_LOCK_HELD": "1"}),
        patch(
            "pynchy.plugins.runtimes.apple_build_lock.os.execvp",
            side_effect=ExecReplacedError,
        ) as execvp,
        pytest.raises(ExecReplacedError),
    ):
        main()

    execvp.assert_called_once_with("tool", ["tool", "arg"])


def test_cli_reports_busy_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYNCHY_APPLE_BUILD_LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(sys, "argv", ["apple-build-lock", "--exec", "tool"])
    monkeypatch.setattr(
        "pynchy.plugins.runtimes.apple_build_lock.fcntl.flock",
        lambda *_args: (_ for _ in ()).throw(BlockingIOError),
    )
    monotonic = iter((0, 0, 0, 61))
    monkeypatch.setattr(
        "pynchy.plugins.runtimes.apple_build_lock.time.monotonic", monotonic.__next__
    )

    assert main() == 75


def test_cli_requires_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["apple-build-lock", "--exec"])

    with pytest.raises(SystemExit):
        main()
