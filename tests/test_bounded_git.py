"""Public source-control boundary checks for bounded Git output."""

from __future__ import annotations

import shutil
import signal
import subprocess  # noqa: S404 - test starts a controlled local Git process.
from unittest.mock import patch

import pytest

from pynchy.host.git_ops import (
    _bounded_git as bg,  # noqa: PLC2701  # allow: private-test-imports - external-process: Git process safety
)
from tests.git_policy_support import git

run_git_bounded_stdout = bg.run_git_bounded_stdout
terminate_unread_process_group = (
    bg._terminate_unread_process_group  # allow: private-test-imports - external-process: kill
)

_GIT = shutil.which("git") or "/usr/bin/git"


def _blocked_git_process() -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603 - fixed local Git command.
        [_GIT, "hash-object", "--stdin"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def test_bounded_git_rejects_negative_output_limits(tmp_path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        run_git_bounded_stdout("--version", max_stdout_bytes=-1, cwd=tmp_path)


def test_bounded_git_captures_success_and_stderr(tmp_path) -> None:
    success = run_git_bounded_stdout("--version", max_stdout_bytes=1024, cwd=tmp_path)
    failure = run_git_bounded_stdout("not-a-git-command", max_stdout_bytes=1024, cwd=tmp_path)

    assert success.returncode == 0
    assert success.stdout.startswith("git version")
    assert failure.returncode != 0
    assert failure.stderr


def test_bounded_git_stops_when_stdout_exceeds_limit(tmp_path) -> None:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.email", "test@test.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "large.txt").write_text("payload\n" * 20_000, encoding="utf-8")
    git(tmp_path, "add", "large.txt")
    git(tmp_path, "commit", "-m", "large output")

    result = run_git_bounded_stdout("show", "HEAD:large.txt", max_stdout_bytes=32, cwd=tmp_path)

    assert result.exceeded_limit is True
    assert len(result.stdout.encode()) == 33


def test_bounded_git_times_out_before_reading_output(tmp_path) -> None:
    result = run_git_bounded_stdout("--version", max_stdout_bytes=1024, timeout=0, cwd=tmp_path)

    assert result.returncode == 124
    assert not result.stdout
    assert result.stderr == "git command timed out after 0 seconds"


def test_bounded_git_uses_direct_child_fallback_when_group_signal_fails() -> None:
    process = _blocked_git_process()

    try:
        with patch(
            "pynchy.host.git_ops._bounded_git.os.killpg",
            side_effect=PermissionError("group unavailable"),
        ) as killpg:
            terminate_unread_process_group(process)

        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    assert process.returncode is not None


def test_bounded_git_kills_child_after_termination_timeout() -> None:
    process = _blocked_git_process()

    def killpg(_pid: int, sig: signal.Signals) -> None:
        if sig == signal.SIGTERM:
            raise ProcessLookupError("group unavailable")
        raise PermissionError("group unavailable")

    try:
        with (
            patch("pynchy.host.git_ops._bounded_git.os.killpg", side_effect=killpg),
            patch.object(
                process,
                "wait",
                side_effect=[subprocess.TimeoutExpired("git", 1), None],
            ),
        ):
            terminate_unread_process_group(process)

    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    assert process.returncode is not None
