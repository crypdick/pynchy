"""Public worktree-registration guards for coordinated Git sync."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures only.
from unittest.mock import patch

from pynchy.host.git_ops.api import ensure_worktree, host_create_pr_from_worktree

pytest_plugins = ("tests.git_policy_support",)


class TestWorktreePublicationPreconditions:
    def test_sync_blocks_an_unregistered_worktree(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        (repo_ctx.worktrees_dir / "agent-1").mkdir(parents=True)

        result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result == {
            "success": False,
            "message": (
                "Publication blocked: isolated worktree is not registered with its repository."
            ),
        }

    def test_sync_reports_an_unresolvable_worktree(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        with patch(
            "pynchy.host.git_ops.worktree_sync.Path.resolve",
            side_effect=OSError("path unavailable"),
        ):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result == {
            "success": False,
            "message": "Could not resolve the isolated worktree path.",
        }

    def test_sync_reports_a_detached_worktree(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)
        worktree_path = repo_ctx.worktrees_dir / "agent-1"

        def run_git(command: str, *args: str, **_kwargs: object):
            if command == "worktree":
                return subprocess.CompletedProcess(
                    ["git", command, *args], 0, f"worktree {worktree_path}\n", ""
                )
            if command == "branch":
                return subprocess.CompletedProcess(["git", command, *args], 0, "", "")
            raise AssertionError(f"unexpected git command: {command}")

        with patch("pynchy.host.git_ops.worktree_sync.run_git", side_effect=run_git):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result == {
            "success": False,
            "message": "Publication blocked: isolated worktree is detached or has no branch.",
        }

    def test_sync_reports_worktree_registry_read_failure(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        def run_git(command: str, *args: str, **_kwargs: object):
            assert (command, args) == ("worktree", ("list", "--porcelain"))
            return subprocess.CompletedProcess(["git", command, *args], 1, "", "failure")

        with patch("pynchy.host.git_ops.worktree_sync.run_git", side_effect=run_git):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert "not registered" in result["message"]

    def test_sync_fails_closed_when_registered_path_cannot_be_resolved(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)
        worktree_path = repo_ctx.worktrees_dir / "agent-1"
        calls = 0

        def resolve(*, strict=False):
            del strict
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("registered path unavailable")
            return worktree_path if calls == 1 else repo_ctx.root

        def run_git(command: str, *args: str, **_kwargs: object):
            assert (command, args) == ("worktree", ("list", "--porcelain"))
            return subprocess.CompletedProcess(
                ["git", command, *args], 0, f"worktree {worktree_path}\n", ""
            )

        with (
            patch("pynchy.host.git_ops.worktree_sync.Path.resolve", side_effect=resolve),
            patch("pynchy.host.git_ops.worktree_sync.run_git", side_effect=run_git),
        ):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert "not registered" in result["message"]

    def test_publication_reports_failed_commit_count(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        with patch("pynchy.host.git_ops.worktree_sync.count_commits", return_value=None):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert "Failed to check commits" in result["message"]
