"""Tests for git_sync helper functions.

Tests _build_rebase_notice(), _get_local_head_sha(), _host_update_main(), and
_host_source_files_changed() — functions with branching logic that aren't
covered by the existing integration tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceDefaultsConfig,
)
from pynchy.git_sync import (
    _build_rebase_notice,
    _get_local_head_sha,
    _host_source_files_changed,
    _host_update_main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
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


# ---------------------------------------------------------------------------
# _build_rebase_notice tests
# ---------------------------------------------------------------------------


class TestBuildRebaseNotice:
    def test_single_commit_shows_message(self, tmp_path):
        """Single commit should show the full commit message."""
        repo = _make_repo(tmp_path)
        old_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        (repo / "feature.txt").write_text("new feature")
        _git(repo, "add", "feature.txt")
        _git(repo, "commit", "-m", "Add cool feature")

        notice = _build_rebase_notice(repo, old_head, 1)
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

        notice = _build_rebase_notice(repo, old_head, 3)
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

        notice = _build_rebase_notice(repo, old_head, 1)
        # Should contain diff stats like "2 files changed"
        assert "file" in notice.lower()
        assert "changed" in notice.lower()

    def test_handles_empty_diff(self, tmp_path):
        """Edge case: same HEAD (no actual diff) should not crash."""
        repo = _make_repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()

        notice = _build_rebase_notice(repo, head, 0)
        assert "Auto-rebased 0 commit(s)" in notice


# ---------------------------------------------------------------------------
# _get_local_head_sha tests
# ---------------------------------------------------------------------------


class TestGetLocalHeadSha:
    def test_returns_sha_for_valid_repo(self, tmp_path):
        """Should return the HEAD SHA of the current repo."""
        repo = _make_repo(tmp_path)
        expected = _git(repo, "rev-parse", "HEAD").stdout.strip()

        s = Settings.model_construct(
            agent=AgentConfig(),
            container=ContainerConfig(),
            server=ServerConfig(),
            logging=LoggingConfig(),
            secrets=SecretsConfig(),
            workspace_defaults=WorkspaceDefaultsConfig(),
            workspaces={},
            commands=CommandWordsConfig(),
            scheduler=SchedulerConfig(),
            intervals=IntervalsConfig(),
            queue=QueueConfig(),
            security=SecurityConfig(),
        )
        s.__dict__["project_root"] = repo
        with patch("pynchy.git_utils.get_settings", return_value=s):
            result = _get_local_head_sha()
            assert result == expected

    def test_returns_empty_string_on_failure(self):
        """Should return empty string when get_head_sha returns 'unknown'."""
        with patch("pynchy.git_sync.get_head_sha", return_value="unknown"):
            result = _get_local_head_sha()
            assert result == ""


# ---------------------------------------------------------------------------
# _host_update_main tests
# ---------------------------------------------------------------------------


class TestHostUpdateMain:
    def test_returns_false_on_fetch_failure(self):
        """Should return False when git fetch fails."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="network error"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = _host_update_main()
            assert result is False

    def test_returns_false_on_rebase_failure(self):
        """Should return False and abort rebase when rebase fails."""
        call_count = 0

        def mock_run(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            cmd_args = args[0] if args else kwargs.get("args", [])
            ok = subprocess.CompletedProcess(args=cmd_args, returncode=0, stdout="", stderr="")
            if "fetch" in cmd_args:
                return ok
            elif "rebase" in cmd_args and "--abort" not in cmd_args:
                return subprocess.CompletedProcess(
                    args=cmd_args, returncode=1, stdout="", stderr="conflict"
                )
            else:
                # rebase --abort
                return ok

        with patch("subprocess.run", side_effect=mock_run):
            result = _host_update_main()
            assert result is False
            # Should have called fetch, rebase, and rebase --abort
            assert call_count >= 3


# ---------------------------------------------------------------------------
# _host_source_files_changed tests
# ---------------------------------------------------------------------------


class TestHostSourceFilesChanged:
    def test_detects_source_changes(self):
        """Should return True when src/ files changed."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="src/pynchy/app.py\n"
        )
        with patch("subprocess.run", return_value=mock_result):
            assert _host_source_files_changed("abc", "def") is True

    def test_no_source_changes(self):
        """Should return False when no src/ files changed."""
        mock_result = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        with patch("subprocess.run", return_value=mock_result):
            assert _host_source_files_changed("abc", "def") is False
