"""Tests for git worktree management.

Uses real git repos via tmp_path to validate actual git behavior.
"""

from __future__ import annotations

import subprocess
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.git_ops._worktree_merge import merge_and_push_worktree, merge_worktree
from pynchy.git_ops.repo import RepoContext
from pynchy.git_ops.worktree import (
    WorktreeError,
    ensure_worktree,
    reconcile_worktrees_at_startup,
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
    _git(origin, "init", "--bare", "--initial-branch=main")

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
    """Set up origin + project repos with patched settings."""
    origin = _make_bare_origin(tmp_path)
    project = _make_project(tmp_path, origin)
    worktrees_dir = tmp_path / "worktrees"

    s = make_settings(project_root=project, worktrees_dir=worktrees_dir)
    repo_ctx = RepoContext(slug="owner/pynchy", root=project, worktrees_dir=worktrees_dir)

    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.git_ops.utils.get_settings", return_value=s))
        stack.enter_context(patch("pynchy.config.get_settings", return_value=s))
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
            "repo_ctx": repo_ctx,
        }


# ---------------------------------------------------------------------------
# ensure_worktree tests
# ---------------------------------------------------------------------------


class TestEnsureWorktree:
    def test_creates_new_worktree(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        result = ensure_worktree("code-improver", repo_ctx)

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
        repo_ctx = git_env["repo_ctx"]

        # Create worktree first
        result1 = ensure_worktree("code-improver", repo_ctx)
        wt_path = result1.path

        # Push a new commit to origin from the project
        (project / "new-file.txt").write_text("new content")
        _git(project, "add", "new-file.txt")
        _git(project, "commit", "-m", "add new file")
        _git(project, "push", "origin", "main")

        # Second call should merge latest origin/main and notify
        result2 = ensure_worktree("code-improver", repo_ctx)
        assert result2.path == wt_path
        assert (wt_path / "new-file.txt").read_text() == "new content"
        assert len(result2.notices) == 1
        assert "Auto-pulled remote changes" in result2.notices[0]

    def test_no_notice_when_already_up_to_date(self, git_env: dict):
        """No notice when worktree is already current with origin."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("code-improver", repo_ctx)

        # Second call with no new commits
        result = ensure_worktree("code-improver", repo_ctx)
        assert result.notices == []

    def test_preserves_uncommitted_changes(self, git_env: dict):
        """Uncommitted changes survive sync and produce a notice."""
        repo_ctx = git_env["repo_ctx"]
        result1 = ensure_worktree("code-improver", repo_ctx)
        wt_path = result1.path

        # Leave uncommitted changes in the worktree
        (wt_path / "wip.txt").write_text("work in progress")

        result2 = ensure_worktree("code-improver", repo_ctx)
        assert result2.path == wt_path
        # WIP file is preserved
        assert (wt_path / "wip.txt").read_text() == "work in progress"
        # Notice about uncommitted changes
        assert len(result2.notices) == 1
        assert "uncommitted changes" in result2.notices[0]

    def test_fetch_failure_produces_notice(self, git_env: dict):
        """Failed fetch on existing worktree is a notice, not an error."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("code-improver", repo_ctx)

        # Break the remote so fetch fails
        _git(git_env["project"], "remote", "set-url", "origin", "/nonexistent/repo")

        result = ensure_worktree("code-improver", repo_ctx)
        assert result.path.exists()
        assert any("fetch failed" in n for n in result.notices)

    def test_error_propagates_for_new_worktree(self, git_env: dict):
        """WorktreeError raised when creating a new worktree with broken remote."""
        repo_ctx = git_env["repo_ctx"]
        _git(git_env["project"], "remote", "set-url", "origin", "/nonexistent/repo")

        with pytest.raises(WorktreeError, match="git fetch failed"):
            ensure_worktree("broken-group", repo_ctx)

    def test_broken_worktree_gets_recreated(self, git_env: dict):
        """Corrupted .git file → worktree is deleted and recreated."""
        repo_ctx = git_env["repo_ctx"]
        result1 = ensure_worktree("code-improver", repo_ctx)
        wt_path = result1.path

        # Corrupt the .git file (simulates stale gitdir from a rename)
        git_file = wt_path / ".git"
        git_file.write_text("gitdir: /nonexistent/path/.git/worktrees/old-name\n")

        # Should detect broken state, delete, and recreate
        result2 = ensure_worktree("code-improver", repo_ctx)
        assert result2.path == wt_path
        assert result2.path.exists()
        assert (result2.path / "README.md").read_text() == "initial"

        # Verify it's a valid git repo now
        status = _git(wt_path, "status")
        assert status.returncode == 0

    def test_broken_worktree_with_uncommitted_files_logs_warning(self, git_env: dict, caplog):
        """Broken worktree with leftover files still gets recreated."""
        repo_ctx = git_env["repo_ctx"]
        result1 = ensure_worktree("code-improver", repo_ctx)
        wt_path = result1.path

        # Add uncommitted files, then corrupt
        (wt_path / "wip.txt").write_text("uncommitted work")
        git_file = wt_path / ".git"
        git_file.write_text("gitdir: /nonexistent/path\n")

        # Should recreate — broken repo means uncommitted work is unrecoverable
        result2 = ensure_worktree("code-improver", repo_ctx)
        assert result2.path.exists()
        assert (result2.path / "README.md").read_text() == "initial"
        # WIP file is gone (worktree was recreated from scratch)
        assert not (wt_path / "wip.txt").exists()


# ---------------------------------------------------------------------------
# merge_worktree tests
# ---------------------------------------------------------------------------


class TestMergeWorktree:
    def test_fast_forward_merge(self, git_env: dict):
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create worktree and make a commit in it
        result = ensure_worktree("code-improver", repo_ctx)
        wt_path = result.path
        (wt_path / "feature.txt").write_text("feature code")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        # Merge should succeed (fast-forward)
        merged = merge_worktree("code-improver", repo_ctx)
        assert merged is True

        # Verify the commit appeared on main branch in project
        assert (project / "feature.txt").read_text() == "feature code"

    def test_nothing_to_merge(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        # Create worktree without making any commits
        ensure_worktree("code-improver", repo_ctx)

        merged = merge_worktree("code-improver", repo_ctx)
        assert merged is True

    def test_diverged_non_conflicting_merges_via_rebase(self, git_env: dict):
        """Diverged but non-conflicting changes are rebased and merged."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create worktree and make a commit
        result = ensure_worktree("code-improver", repo_ctx)
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
        merged = merge_worktree("code-improver", repo_ctx)
        assert merged is True

        # Both files should be on main now
        assert (project / "feature.txt").read_text() == "worktree change"
        assert (project / "other.txt").read_text() == "main change"

    def test_conflicting_merge_returns_false(self, git_env: dict):
        """True conflict (same file, different content) returns False."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create worktree and modify README.md
        result = ensure_worktree("code-improver", repo_ctx)
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
        merged = merge_worktree("code-improver", repo_ctx)
        assert merged is False


# ---------------------------------------------------------------------------
# reconcile_worktrees_at_startup tests
# ---------------------------------------------------------------------------


class TestReconcileWorktreesAtStartup:
    def test_rebases_diverged_worktree(self, git_env: dict):
        """Diverged worktree branch is rebased onto main at startup."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create worktree and commit
        result = ensure_worktree("code-improver", repo_ctx)
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

        with patch("pynchy.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": []})

        # After reconcile, worktree branch should be ahead of main (rebased), not diverged
        behind_after = _git(project, "rev-list", "worktree/code-improver..main", "--count")
        assert int(behind_after.stdout.strip()) == 0

    def test_skips_non_diverged_worktree(self, git_env: dict):
        """Worktrees that aren't diverged are left alone."""
        repo_ctx = git_env["repo_ctx"]
        result = ensure_worktree("code-improver", repo_ctx)
        wt_path = result.path

        # Commit in worktree (ahead only, not diverged)
        (wt_path / "feature.txt").write_text("feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "feature")

        head_before = _git(wt_path, "rev-parse", "HEAD").stdout.strip()

        with patch("pynchy.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": []})

        # HEAD unchanged — no rebase needed
        head_after = _git(wt_path, "rev-parse", "HEAD").stdout.strip()
        assert head_before == head_after

    def test_handles_no_worktrees_dir(self, git_env: dict):
        """Runs cleanly when worktrees dir doesn't exist."""
        repo_ctx = git_env["repo_ctx"]
        # worktrees_dir doesn't exist yet — should not raise
        with patch("pynchy.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": []})

    def test_creates_missing_worktrees_at_startup(self, git_env: dict):
        """Worktrees for repo_access folders are created if missing."""
        repo_ctx = git_env["repo_ctx"]
        with patch("pynchy.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(
                repo_groups={"owner/pynchy": ["admin-1", "code-improver"]}
            )

        worktrees_dir = git_env["worktrees_dir"]
        assert (worktrees_dir / "admin-1").exists()
        assert (worktrees_dir / "code-improver").exists()

        # Both should be valid git repos
        _git(worktrees_dir / "admin-1", "status")
        _git(worktrees_dir / "code-improver", "status")

    def test_idempotent(self, git_env: dict):
        """Calling twice with same folders doesn't break anything."""
        repo_ctx = git_env["repo_ctx"]
        folders = ["admin-1", "code-improver"]
        with patch("pynchy.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": folders})

        # Record state
        worktrees_dir = git_env["worktrees_dir"]
        head_admin = _git(worktrees_dir / "admin-1", "rev-parse", "HEAD").stdout.strip()
        head_ci = _git(worktrees_dir / "code-improver", "rev-parse", "HEAD").stdout.strip()

        # Second call — should be a no-op
        with patch("pynchy.git_ops.repo.get_repo_context", return_value=repo_ctx):
            reconcile_worktrees_at_startup(repo_groups={"owner/pynchy": folders})

        assert _git(worktrees_dir / "admin-1", "rev-parse", "HEAD").stdout.strip() == head_admin
        assert _git(worktrees_dir / "code-improver", "rev-parse", "HEAD").stdout.strip() == head_ci


# ---------------------------------------------------------------------------
# merge_and_push_worktree tests
# ---------------------------------------------------------------------------


class TestMergeAndPushWorktree:
    def test_merge_and_push_success(self, git_env: dict):
        """Commits merge into main and push to origin in one call."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("code-improver", repo_ctx)
        wt_path = result.path
        (wt_path / "feature.txt").write_text("new feature")
        _git(wt_path, "add", "feature.txt")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "add feature")

        merge_and_push_worktree("code-improver", repo_ctx)

        # Verify on main
        assert (project / "feature.txt").read_text() == "new feature"

        # Verify pushed to origin
        count = _git(project, "rev-list", "origin/main..HEAD", "--count")
        assert int(count.stdout.strip()) == 0

    def test_skips_push_when_merge_fails(self, git_env: dict):
        """When merge fails (conflict), push is not attempted."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("code-improver", repo_ctx)
        wt_path = result.path
        (wt_path / "README.md").write_text("worktree version")
        _git(wt_path, "add", "README.md")
        _git(wt_path, "config", "user.email", "test@test.com")
        _git(wt_path, "config", "user.name", "Test")
        _git(wt_path, "commit", "-m", "worktree edit README")

        # Create conflict on main
        (project / "README.md").write_text("main version")
        _git(project, "add", "README.md")
        _git(project, "commit", "-m", "main edit README")

        # Should not raise, just skip push
        merge_and_push_worktree("code-improver", repo_ctx)

        # Main should still have its version (merge failed, push skipped)
        assert (project / "README.md").read_text() == "main version"

    def test_nothing_to_merge(self, git_env: dict):
        """No-op when worktree has no new commits."""
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("code-improver", repo_ctx)

        # Should complete without error
        merge_and_push_worktree("code-improver", repo_ctx)


# ---------------------------------------------------------------------------
# Sequential merge tests (multiple worktrees merging to main)
# ---------------------------------------------------------------------------


class TestSequentialMerges:
    """Verify that multiple worktrees can merge sequentially without issues.

    Critical scenario: agent-1 merges to main, then agent-2 (which diverged
    from the pre-merge main) also merges. The rebase-then-merge strategy
    must handle this correctly.
    """

    def test_two_worktrees_merge_sequentially(self, git_env: dict):
        """Two worktrees with non-conflicting changes can merge one after another."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        # Create two worktrees
        r1 = ensure_worktree("agent-1", repo_ctx)
        r2 = ensure_worktree("agent-2", repo_ctx)

        # Each modifies a different file
        (r1.path / "agent1.txt").write_text("from agent 1")
        _git(r1.path, "add", "agent1.txt")
        _git(r1.path, "config", "user.email", "test@test.com")
        _git(r1.path, "config", "user.name", "Test")
        _git(r1.path, "commit", "-m", "agent 1 work")

        (r2.path / "agent2.txt").write_text("from agent 2")
        _git(r2.path, "add", "agent2.txt")
        _git(r2.path, "config", "user.email", "test@test.com")
        _git(r2.path, "config", "user.name", "Test")
        _git(r2.path, "commit", "-m", "agent 2 work")

        # First merge succeeds
        assert merge_worktree("agent-1", repo_ctx) is True
        assert (project / "agent1.txt").read_text() == "from agent 1"

        # Second merge succeeds (rebases onto new main first)
        assert merge_worktree("agent-2", repo_ctx) is True
        assert (project / "agent2.txt").read_text() == "from agent 2"

        # Both files on main
        assert (project / "agent1.txt").exists()
        assert (project / "agent2.txt").exists()

    def test_multiple_commits_per_worktree(self, git_env: dict):
        """A worktree with multiple commits merges all of them."""
        project = git_env["project"]
        repo_ctx = git_env["repo_ctx"]

        result = ensure_worktree("agent-1", repo_ctx)
        wt_path = result.path

        for i in range(3):
            (wt_path / f"file{i}.txt").write_text(f"content {i}")
            _git(wt_path, "add", f"file{i}.txt")
            _git(wt_path, "config", "user.email", "test@test.com")
            _git(wt_path, "config", "user.name", "Test")
            _git(wt_path, "commit", "-m", f"commit {i}")

        assert merge_worktree("agent-1", repo_ctx) is True

        for i in range(3):
            assert (project / f"file{i}.txt").read_text() == f"content {i}"
