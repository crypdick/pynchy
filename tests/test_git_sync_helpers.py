"""Tests for git_sync helper functions.

Tests build_rebase_notice(), get_local_head_sha(), host_update_main(), and
host_source_files_changed() — functions with branching logic that aren't
covered by the existing integration tests.
"""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - test helpers mock subprocess behavior and exceptions
from typing import TYPE_CHECKING
from unittest.mock import patch

from conftest import make_settings

from pynchy.host.git_ops import build_rebase_notice
from pynchy.host.git_ops.sync_poll import (
    get_local_head_sha,
    host_source_files_changed,
    host_update_main,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603, S607, RUF100 - test helper runs fixed git argv against temp repos
        ["git", *args],  # noqa: S607, RUF100 - test helper deliberately resolves git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Create a simple git repo with one commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("initial")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit")
    return repo


def _flat_commands(commands: list[list[str]]) -> list[str]:
    return [" ".join(command) for command in commands]


def _first_command_index(commands: list[str], *fragments: str) -> int:
    return next(
        index
        for index, command in enumerate(commands)
        if all(fragment in command for fragment in fragments)
    )


# ---------------------------------------------------------------------------
# build_rebase_notice tests
# ---------------------------------------------------------------------------


class TestBuildRebaseNotice:
    def test_single_commit_shows_message(self, tmp_path):
        """Single commit should show the full commit message."""
        repo = _make_repo(tmp_path)
        old_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "feature.txt").write_text("new feature")
        _git(repo, "add", "feature.txt")
        _git(repo, "commit", "-m", "Add cool feature")

        notice = build_rebase_notice(repo, old_head, 1)
        assert "Auto-rebased 1 commit(s)" in notice
        assert "Add cool feature" in notice
        assert "--oneline" not in notice

    def test_multiple_commits_shows_oneline_hint(self, tmp_path):
        """Multiple commits should show hint to run git log."""
        repo = _make_repo(tmp_path)
        old_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        for i in range(3):
            (repo / f"file{i}.txt").write_text(f"content {i}")
            _git(repo, "add", f"file{i}.txt")
            _git(repo, "commit", "-m", f"Change {i}")

        notice = build_rebase_notice(repo, old_head, 3)
        assert "Auto-rebased 3 commit(s)" in notice
        assert "--oneline" in notice

    def test_includes_file_change_stats(self, tmp_path):
        """Should include file change statistics."""
        repo = _make_repo(tmp_path)
        old_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "a.txt").write_text("aaa")
        (repo / "b.txt").write_text("bbb")
        _git(repo, "add", "a.txt", "b.txt")
        _git(repo, "commit", "-m", "Add two files")

        notice = build_rebase_notice(repo, old_head, 1)
        # Should contain diff stats like "2 files changed"
        assert "file" in notice.lower()
        assert "changed" in notice.lower()

    def test_handles_empty_diff(self, tmp_path):
        """Edge case: same HEAD (no actual diff) should not crash."""
        repo = _make_repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        notice = build_rebase_notice(repo, head, 0)
        assert "Auto-rebased 0 commit(s)" in notice


# ---------------------------------------------------------------------------
# get_local_head_sha tests
# ---------------------------------------------------------------------------


class TestGetLocalHeadSha:
    def test_returns_sha_for_valid_repo(self, tmp_path):
        """Should return the HEAD SHA of the current repo."""
        repo = _make_repo(tmp_path)
        expected = _git(repo, "rev-parse", "HEAD").stdout.strip()

        s = make_settings(project_root=repo)
        with patch("pynchy.host.git_ops.utils.get_settings", return_value=s):
            result = get_local_head_sha()
            assert result == expected

    def test_returns_empty_string_on_failure(self):
        """Should return empty string when get_head_sha returns 'unknown'."""
        with patch("pynchy.host.git_ops.sync_poll.get_head_sha", return_value="unknown"):
            result = get_local_head_sha()
            assert not result


# ---------------------------------------------------------------------------
# host_update_main tests
# ---------------------------------------------------------------------------


class TestHostUpdateMain:
    def test_returns_false_on_fetch_failure(self, tmp_path: Path):
        """Should return False when git fetch fails."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="network error"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = host_update_main(tmp_path)
            assert result is False

    def test_returns_false_on_rebase_failure(self, tmp_path: Path):
        """Should return False and abort rebase when rebase fails."""
        (tmp_path / ".git").mkdir()
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd_args = args[0] if args else kwargs.get("args", [])
            ok = subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout="", stderr="")
            if "fetch" in cmd_args:
                return ok
            if "rebase" in cmd_args and "--abort" not in cmd_args:
                return subprocess.CompletedProcess(
                    args=cmd_args, returncode=1, stdout="", stderr="conflict"
                )
            # rebase --abort, status --porcelain, etc.
            return ok

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
        ):
            result = host_update_main(tmp_path)
            assert result is False
            # Should have called status, fetch, rebase, and rebase --abort
            assert call_count >= 3

    def test_aborts_stale_rebase_before_fetch(self, tmp_path: Path):
        """Stale rebase-merge dir triggers rebase --abort before fetch."""
        (tmp_path / ".git" / "rebase-merge").mkdir(parents=True)
        commands: list[list[str]] = []

        def mock_run(*args, **kwargs):
            cmd_args = args[0] if args else kwargs.get("args", [])
            commands.append(list(cmd_args))
            return subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout="", stderr="")

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
        ):
            result = host_update_main(tmp_path)

        assert result is True
        flat = [" ".join(c) for c in commands]
        abort_idx = next(i for i, f in enumerate(flat) if "rebase" in f and "--abort" in f)
        fetch_idx = next(i for i, f in enumerate(flat) if "fetch" in f)
        assert abort_idx < fetch_idx, "rebase --abort must come before fetch"

    def test_stashes_dirty_tree_before_fetch(self, tmp_path: Path):
        """Dirty working tree is stashed, then restored after success."""
        (tmp_path / ".git").mkdir()
        commands: list[list[str]] = []

        def mock_run(*args, **kwargs):
            cmd_args = args[0] if args else kwargs.get("args", [])
            commands.append(list(cmd_args))
            if "status" in cmd_args and "--porcelain" in cmd_args:
                return subprocess.CompletedProcess(
                    args=cmd_args, returncode=0, stdout="M dirty.txt\n", stderr=""
                )
            return subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout="", stderr="")

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
            patch("pynchy.host.git_ops.sync_poll.push_local_commits", return_value=True),
        ):
            result = host_update_main(tmp_path)

        assert result is True
        flat = _flat_commands(commands)
        stash_idx = _first_command_index(flat, "stash", "--include-untracked")
        fetch_idx = _first_command_index(flat, "fetch")
        pop_idx = _first_command_index(flat, "stash", "pop")
        assert stash_idx < fetch_idx, "stash must come before fetch"
        assert fetch_idx < pop_idx, "fetch must come before stash pop"
        # When stash pop succeeds, no marker commit should be created
        assert not any("commit" in f and "--allow-empty" in f for f in flat), (
            "no marker commit on clean pop"
        )

    def test_returns_false_on_stash_pop_conflict(self, tmp_path: Path):
        """A conflicted stash restore must not be reported as a deployable update."""
        (tmp_path / ".git").mkdir()
        commands: list[list[str]] = []

        def mock_run(*args, **kwargs):
            cmd_args = args[0] if args else kwargs.get("args", [])
            commands.append(list(cmd_args))
            if "status" in cmd_args and "--porcelain" in cmd_args:
                return subprocess.CompletedProcess(
                    args=cmd_args, returncode=0, stdout="M dirty.txt\n", stderr=""
                )
            if "stash" in cmd_args and "pop" in cmd_args:
                return subprocess.CompletedProcess(
                    args=cmd_args, returncode=1, stdout="", stderr="CONFLICT (content)"
                )
            return subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout="", stderr="")

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
            patch("pynchy.host.git_ops.sync_poll.push_local_commits") as push_local,
        ):
            result = host_update_main(tmp_path)

        assert result is False
        flat = _flat_commands(commands)
        assert _first_command_index(flat, "stash", "pop") >= 0
        assert not any("commit" in command and "--allow-empty" in command for command in flat)
        assert push_local.call_count == 1

    def test_clean_tree_skips_recovery(self, tmp_path: Path):
        """Clean tree with no stale state goes straight to fetch."""
        (tmp_path / ".git").mkdir()
        commands: list[list[str]] = []

        def mock_run(*args, **kwargs):
            cmd_args = args[0] if args else kwargs.get("args", [])
            commands.append(list(cmd_args))
            return subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout="", stderr="")

        with (
            patch("subprocess.run", side_effect=mock_run),
            patch("pynchy.host.git_ops.sync_poll.detect_main_branch", return_value="main"),
        ):
            result = host_update_main(tmp_path)

        assert result is True
        flat = [" ".join(c) for c in commands]
        assert not any("rebase" in f and "--abort" in f for f in flat), "no abort needed"
        assert not any("stash" in f for f in flat), "no stash needed"
        assert any("status" in f for f in flat), "status check must happen"
        assert any("fetch" in f for f in flat), "fetch must happen"


# ---------------------------------------------------------------------------
# host_source_files_changed tests
# ---------------------------------------------------------------------------


class TestHostSourceFilesChanged:
    def test_detects_source_changes(self):
        """Should return True when src/ files changed."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="src/pynchy/app.py\n"
        )
        with patch("subprocess.run", return_value=mock_result):
            assert host_source_files_changed("abc", "def") is True

    def test_no_source_changes(self):
        """Should return False when no src/ files changed."""
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            assert host_source_files_changed("abc", "def") is False
