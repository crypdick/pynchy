"""Tests for git worktree management.

Uses real git repos via tmp_path to validate actual git behavior.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.worktree import (
    WorktreeError,
    cleanup_stale_worktrees,
    ensure_worktree,
    merge_worktree,
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


def _make_bare_origin(tmp_path: Path) -> Path:
    """Create a bare 'origin' repo with one commit on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")

    # Clone, commit, push to set up origin/main
    clone = tmp_path / "setup-clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README.md").write_text("initial")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "initial commit")
    _git(clone, "push", "origin", "main")
    return origin


def _make_project(tmp_path: Path, origin: Path) -> Path:
    """Clone origin into a 'project' directory (simulates PROJECT_ROOT)."""
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    return project


@pytest.fixture
def git_env(tmp_path: Path):
    """Set up origin + project repos, patching PROJECT_ROOT and WORKTREES_DIR."""
    origin = _make_bare_origin(tmp_path)
    project = _make_project(tmp_path, origin)
    worktrees_dir = tmp_path / "worktrees"

    with (
        patch("pynchy.worktree.PROJECT_ROOT", project),
        patch("pynchy.worktree.WORKTREES_DIR", worktrees_dir),
    ):
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
        }


# ---------------------------------------------------------------------------
# ensure_worktree tests
# ---------------------------------------------------------------------------


class TestEnsureWorktree:
    def test_creates_new_worktree(self, git_env: dict):
        result = ensure_worktree("code-improver")

        assert result.path == git_env["worktrees_dir"] / "code-improver"
        assert result.path.exists()
        assert (result.path / "README.md").read_text() == "initial"
        assert result.notices == []

        # Verify the branch was created
        branch_list = _git(git_env["project"], "branch", "--list", "worktree/code-improver")
        assert "worktree/code-improver" in branch_list.stdout

    def test_syncs_existing_worktree_with_notice(self, git_env: dict):
        """Pulling new commits produces a notice about auto-pulled changes."""
        project = git_env["project"]

        # Create worktree first
        result1 = ensure_worktree("code-improver")
        wt_path = result1.path

        # Push a new commit to origin from the project
        (project / "new-file.txt").write_text("new content")
        _git(project, "add", "new-file.txt")
        _git(project, "commit", "-m", "add new file")
        _git(project, "push", "origin", "main")

        # Second call should merge latest origin/main and notify
        result2 = ensure_worktree("code-improver")
        assert result2.path == wt_path
        assert (wt_path / "new-file.txt").read_text() == "new content"
        assert len(result2.notices) == 1
        assert "Auto-pulled remote changes" in result2.notices[0]

    def test_no_notice_when_already_up_to_date(self, git_env: dict):
        """No notice when worktree is already current with origin."""
        ensure_worktree("code-improver")

        # Second call with no new commits
        result = ensure_worktree("code-improver")
        assert result.notices == []

    def test_preserves_uncommitted_changes(self, git_env: dict):
        """Uncommitted changes survive sync and produce a notice."""
        result1 = ensure_worktree("code-improver")
        wt_path = result1.path

        # Leave uncommitted changes in the worktree
        (wt_path / "wip.txt").write_text("work in progress")

        result2 = ensure_worktree("code-improver")
        assert result2.path == wt_path
        # WIP file is preserved
        assert (wt_path / "wip.txt").read_text() == "work in progress"
        # Notice about uncommitted changes
        assert len(result2.notices) == 1
        assert "uncommitted changes" in result2.notices[0]

    def test_fetch_failure_produces_notice(self, git_env: dict):
        """Failed fetch on existing worktree is a notice, not an error."""
        ensure_worktree("code-improver")

        # Break the remote so fetch fails
        _git(git_env["project"], "remote", "set-url", "origin", "/nonexistent/repo")

        result = ensure_worktree("code-improver")
        assert result.path.exists()
        assert any("fetch failed" in n for n in result.notices)

    def test_error_propagates_for_new_worktree(self, git_env: dict):
        """WorktreeError raised when creating a new worktree with broken remote."""
        _git(git_env["project"], "remote", "set-url", "origin", "/nonexistent/repo")

        with pytest.raises(WorktreeError, match="git fetch failed"):
            ensure_worktree("broken-group")


# ---------------------------------------------------------------------------
# merge_worktree tests
# ---------------------------------------------------------------------------


class TestMergeWorktree:
    def test_fast_forward_merge(self, git_env: dict):
        project = git_env["project"]

        # Create worktree and make a commit in it
        result = ensure_worktree("code-improver")
        wt_path = result.path
        (wt_path / "feature.txt").write_text("feature code")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        # Merge should succeed (fast-forward)
        merged = merge_worktree("code-improver")
        assert merged is True

        # Verify the commit appeared on main branch in project
        assert (project / "feature.txt").read_text() == "feature code"

    def test_nothing_to_merge(self, git_env: dict):
        # Create worktree without making any commits
        ensure_worktree("code-improver")

        merged = merge_worktree("code-improver")
        assert merged is True

    def test_diverged_non_conflicting_merges_via_rebase(self, git_env: dict):
        """Diverged but non-conflicting changes are rebased and merged."""
        project = git_env["project"]

        # Create worktree and make a commit
        result = ensure_worktree("code-improver")
        wt_path = result.path
        (wt_path / "feature.txt").write_text("worktree change")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "worktree commit")

        # Make a divergent commit on main in project (different file = no conflict)
        (project / "other.txt").write_text("main change")
        _git(project, "add", "other.txt")
        _git(project, "commit", "-m", "divergent commit on main")

        # Rebase-then-merge handles diverged branches
        merged = merge_worktree("code-improver")
        assert merged is True

        # Both files should be on main now
        assert (project / "feature.txt").read_text() == "worktree change"
        assert (project / "other.txt").read_text() == "main change"

    def test_conflicting_merge_returns_false(self, git_env: dict):
        """True conflict (same file, different content) returns False."""
        project = git_env["project"]

        # Create worktree and modify README.md
        result = ensure_worktree("code-improver")
        wt_path = result.path
        (wt_path / "README.md").write_text("worktree version")
        _git(wt_path, "add", "README.md")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "worktree edit README")

        # Make a conflicting commit on main (same file, different content)
        (project / "README.md").write_text("main version")
        _git(project, "add", "README.md")
        _git(project, "commit", "-m", "main edit README")

        # Should fail due to real conflict
        merged = merge_worktree("code-improver")
        assert merged is False


# ---------------------------------------------------------------------------
# cleanup_stale_worktrees tests
# ---------------------------------------------------------------------------


class TestCleanupStaleWorktrees:
    def test_rebases_diverged_worktree(self, git_env: dict):
        """Diverged worktree branch is rebased onto main at startup."""
        project = git_env["project"]

        # Create worktree and commit
        result = ensure_worktree("code-improver")
        wt_path = result.path
        (wt_path / "feature.txt").write_text("worktree work")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "worktree commit")

        # Advance main to create divergence
        (project / "other.txt").write_text("main work")
        _git(project, "add", "other.txt")
        _git(project, "commit", "-m", "advance main")

        # Verify divergence exists
        ahead = _git(project, "rev-list", "main..worktree/code-improver", "--count")
        behind = _git(project, "rev-list", "worktree/code-improver..main", "--count")
        assert int(ahead.stdout.strip()) > 0
        assert int(behind.stdout.strip()) > 0

        cleanup_stale_worktrees()

        # After cleanup, worktree branch should be ahead of main (rebased), not diverged
        behind_after = _git(project, "rev-list", "worktree/code-improver..main", "--count")
        assert int(behind_after.stdout.strip()) == 0

    def test_skips_non_diverged_worktree(self, git_env: dict):
        """Worktrees that aren't diverged are left alone."""
        result = ensure_worktree("code-improver")
        wt_path = result.path

        # Commit in worktree (ahead only, not diverged)
        (wt_path / "feature.txt").write_text("feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "feature")

        head_before = _git(wt_path, "rev-parse", "HEAD").stdout.strip()

        cleanup_stale_worktrees()

        # HEAD unchanged — no rebase needed
        head_after = _git(wt_path, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after

    def test_handles_no_worktrees_dir(self, git_env: dict):
        """Runs cleanly when worktrees dir doesn't exist."""
        # worktrees_dir doesn't exist yet — should not raise
        cleanup_stale_worktrees()
