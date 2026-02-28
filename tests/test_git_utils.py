"""Tests for git_utils.py — shared git helpers.

These functions are used by worktree and git_sync modules.
They handle critical operations like pushing commits and detecting repo state,
with retry logic and error recovery that warrant thorough testing.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.utils import (
    GitCommandError,
    count_unpushed_commits,
    detect_main_branch,
    files_changed_between,
    get_head_sha,
    is_repo_dirty,
    push_local_commits,
    require_success,
)


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Helper: simulate a successful git command."""
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _fail(stderr: str = "error") -> subprocess.CompletedProcess[str]:
    """Helper: simulate a failed git command."""
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


# ---------------------------------------------------------------------------
# detect_main_branch
# ---------------------------------------------------------------------------


class TestDetectMainBranch:
    def test_parses_branch_from_symbolic_ref(self):
        with patch(
            "pynchy.host.git_ops.utils.run_git", return_value=_ok("refs/remotes/origin/main\n")
        ):
            assert detect_main_branch() == "main"

    def test_parses_non_standard_branch_name(self):
        with patch(
            "pynchy.host.git_ops.utils.run_git", return_value=_ok("refs/remotes/origin/master\n")
        ):
            assert detect_main_branch() == "master"

    def test_falls_back_to_main_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert detect_main_branch() == "main"


# ---------------------------------------------------------------------------
# get_head_sha
# ---------------------------------------------------------------------------


class TestGetHeadSha:
    def test_returns_sha_on_success(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("abc123\n")):
            assert get_head_sha() == "abc123"

    def test_returns_unknown_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert get_head_sha() == "unknown"

    def test_returns_unknown_on_exception(self):
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError("no git")):
            assert get_head_sha() == "unknown"


# ---------------------------------------------------------------------------
# is_repo_dirty
# ---------------------------------------------------------------------------


class TestIsRepoDirty:
    def test_dirty_when_porcelain_has_output(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok(" M file.py\n")):
            assert is_repo_dirty() is True

    def test_clean_when_porcelain_is_empty(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("")):
            assert is_repo_dirty() is False

    def test_clean_when_whitespace_only(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("  \n")):
            assert is_repo_dirty() is False

    def test_returns_false_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert is_repo_dirty() is False

    def test_returns_false_on_exception(self):
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError):
            assert is_repo_dirty() is False


# ---------------------------------------------------------------------------
# count_unpushed_commits
# ---------------------------------------------------------------------------


class TestCountUnpushedCommits:
    def test_returns_count_on_success(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok("3\n"),  # rev-list --count
            ]
            assert count_unpushed_commits() == 3

    def test_returns_zero_when_nothing_to_push(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),
                _ok("0\n"),
            ]
            assert count_unpushed_commits() == 0

    def test_returns_zero_on_rev_list_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),
                _fail(),
            ]
            assert count_unpushed_commits() == 0

    def test_returns_zero_on_os_error(self):
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError):
            assert count_unpushed_commits() == 0

    def test_returns_zero_on_subprocess_timeout(self):
        with patch(
            "pynchy.host.git_ops.utils.run_git",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            assert count_unpushed_commits() == 0

    def test_handles_empty_stdout(self):
        """Empty rev-list output should be treated as 0 via the `or '0'` guard."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),
                _ok(""),
            ]
            assert count_unpushed_commits() == 0


# ---------------------------------------------------------------------------
# files_changed_between
# ---------------------------------------------------------------------------


class TestFilesChangedBetween:
    def test_returns_true_when_files_changed(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("src/app.py\n")):
            assert files_changed_between("aaa", "bbb", "src/") is True

    def test_returns_false_when_no_files_changed(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_ok("")):
            assert files_changed_between("aaa", "bbb", "src/") is False

    def test_returns_false_on_failure(self):
        with patch("pynchy.host.git_ops.utils.run_git", return_value=_fail()):
            assert files_changed_between("aaa", "bbb", "src/") is False


# ---------------------------------------------------------------------------
# push_local_commits — the most complex function with retry logic
# ---------------------------------------------------------------------------


class TestPushLocalCommits:
    """Tests for push_local_commits, which has a two-attempt rebase+push
    strategy to handle the race where origin advances between fetch and rebase.
    """

    def test_nothing_to_push_returns_true(self):
        """When rev-list shows 0 commits ahead, nothing to do."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("0\n"),  # rev-list --count
            ]
            assert push_local_commits() is True

    def test_rev_list_failure_returns_true(self):
        """When rev-list fails, assume nothing to push (can't tell)."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _fail(),  # rev-list fails
            ]
            assert push_local_commits() is True

    def test_successful_rebase_and_push(self):
        """Happy path: fetch, rebase, push all succeed."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("2\n"),  # rev-list: 2 commits ahead
                _ok(),  # rebase succeeds
                _ok(),  # push succeeds
            ]
            assert push_local_commits() is True

    def test_fetch_failure_returns_false(self):
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _fail("fetch error"),
            ]
            assert push_local_commits() is False

    def test_rebase_fails_then_succeeds_on_retry(self):
        """First rebase fails (origin advanced), retry with fresh fetch succeeds."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # initial fetch
                _ok("1\n"),  # rev-list: 1 commit ahead
                _fail("conflict"),  # rebase fails
                _ok(),  # rebase --abort
                _ok(),  # retry fetch
                _ok(),  # rebase succeeds on second attempt
                _ok(),  # push succeeds
            ]
            assert push_local_commits() is True

    def test_rebase_fails_twice_returns_false(self):
        """Both rebase attempts fail — exhausted retries."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # initial fetch
                _ok("1\n"),  # rev-list
                _fail("conflict"),  # first rebase fails
                _ok(),  # rebase --abort
                _ok(),  # retry fetch
                _fail("still conflict"),  # second rebase fails
                _ok(),  # rebase --abort (second)
            ]
            assert push_local_commits() is False

    def test_retry_fetch_fails_returns_false(self):
        """First rebase fails, and the retry fetch also fails."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # initial fetch
                _ok("1\n"),  # rev-list
                _fail(),  # rebase fails
                _ok(),  # rebase --abort
                _fail("network error"),  # retry fetch fails
            ]
            assert push_local_commits() is False

    def test_push_failure_returns_false(self):
        """Rebase succeeds but push fails."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok(),  # fetch
                _ok("1\n"),  # rev-list
                _ok(),  # rebase succeeds
                _fail("push rejected"),  # push fails
            ]
            assert push_local_commits() is False

    def test_skip_fetch_skips_initial_fetch(self):
        """skip_fetch=True goes straight to rev-list (after detect_main_branch)."""
        with patch("pynchy.host.git_ops.utils.run_git") as mock:
            mock.side_effect = [
                _ok("refs/remotes/origin/main\n"),  # detect_main_branch
                _ok("0\n"),  # rev-list (no fetch before this)
            ]
            assert push_local_commits(skip_fetch=True) is True
            # Verify only detect_main_branch + rev-list were called (no fetch)
            assert mock.call_count == 2
            assert "rev-list" in mock.call_args[0]

    def test_subprocess_timeout_returns_false(self):
        """Subprocess errors (e.g. timeout) are caught and return False."""
        with patch(
            "pynchy.host.git_ops.utils.run_git",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
        ):
            assert push_local_commits() is False

    def test_os_error_returns_false(self):
        """OS-level errors (e.g. git not found) are caught and return False."""
        with patch("pynchy.host.git_ops.utils.run_git", side_effect=OSError("No such file")):
            assert push_local_commits() is False


# ---------------------------------------------------------------------------
# require_success and GitCommandError
# ---------------------------------------------------------------------------


class TestRequireSuccess:
    """Tests for the require_success helper that enforces git command success."""

    def test_returns_stripped_stdout_on_success(self):
        result = _ok("  abc123  \n")
        assert require_success(result, "rev-parse") == "abc123"

    def test_returns_empty_string_for_empty_stdout(self):
        result = _ok("")
        assert require_success(result, "status") == ""

    def test_raises_git_command_error_on_failure(self):
        result = _fail("fatal: not a git repo")
        with pytest.raises(GitCommandError) as exc_info:
            require_success(result, "status --porcelain")
        assert exc_info.value.command == "status --porcelain"
        assert exc_info.value.stderr == "fatal: not a git repo"
        assert exc_info.value.returncode == 1

    def test_error_message_formatting(self):
        result = _fail("error: pathspec 'x' did not match")
        with pytest.raises(GitCommandError) as exc_info:
            require_success(result, "checkout x")
        msg = str(exc_info.value)
        assert "git checkout x failed" in msg
        assert "exit 1" in msg
        assert "pathspec" in msg

    def test_non_zero_exit_codes(self):
        """Test various non-zero exit codes."""
        for code in (1, 2, 128):
            result = subprocess.CompletedProcess([], code, stdout="", stderr="err")
            with pytest.raises(GitCommandError) as exc_info:
                require_success(result, "test")
            assert exc_info.value.returncode == code
