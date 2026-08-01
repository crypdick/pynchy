"""Public source-control boundary checks for bounded Git output."""

from __future__ import annotations

import pytest

from pynchy.host.git_ops import (
    _bounded_git,  # noqa: PLC2701  # allow: private-test-imports - external-process: Git process safety
)
from tests.git_policy_support import git

run_git_bounded_stdout = _bounded_git.run_git_bounded_stdout


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
