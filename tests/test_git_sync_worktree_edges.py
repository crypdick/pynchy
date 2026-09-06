"""Public worktree-registration guards for coordinated Git sync."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures only.
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import ensure_worktree, host_create_pr_from_worktree, run_git
from tests.git_policy_support import git

pytest_plugins = ("tests.git_policy_support",)


@pytest.mark.parametrize(
    ("published", "proposed", "expected"),
    [
        ("syn/233/original-title", "syn/233/revised-title", "syn/233/original-title"),
        ("syn/234/original-title", "syn/233/revised-title", "syn/233/revised-title"),
        ("syn/233/original-title", "feature/revised", "feature/revised"),
        ("syn/233/original-title", "team/feature/revised", "team/feature/revised"),
        (None, "syn/233/revised-title", "syn/233/revised-title"),
    ],
)
def test_revised_title_keeps_only_same_issue_upstream(
    git_env: dict, published: str | None, proposed: str, expected: str
):
    repo_ctx = git_env["repo_ctx"]
    worktree = ensure_worktree("agent-1", repo_ctx).path
    git(worktree, "commit", "--allow-empty", "-m", "Initial work")
    if published is not None:
        git(worktree, "push", "-u", "origin", f"HEAD:{published}")
    else:
        git(worktree, "branch", "--unset-upstream")
    git(worktree, "commit", "--allow-empty", "-m", "Review correction")
    expected_head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    real_run = subprocess.run

    def provider(args, **kwargs):
        if args[0] != "gh":
            return real_run(args, **kwargs)
        assert args[1:3] == ["pr", "list"], "Review must not create a second PR"
        assert f"--head={expected}" in args
        return subprocess.CompletedProcess(args, 0, "https://github.com/owner/repo/pull/1\n", "")

    with patch("pynchy.host.git_ops.sync.subprocess.run", side_effect=provider):
        result = host_create_pr_from_worktree(
            "agent-1",
            repo_ctx,
            publication_branch=proposed,
            pr_title="Revised title",
            pr_body="Review correction",
        )

    assert result["pr_url"] == "https://github.com/owner/repo/pull/1"
    assert git(git_env["origin"], "rev-parse", expected).stdout.strip() == expected_head
    if expected != proposed:
        assert proposed not in git(git_env["origin"], "branch").stdout
    if published is not None and published != expected:
        assert git(git_env["origin"], "rev-parse", published).stdout.strip() != expected_head


class TestWorktreePublicationPreconditions:
    def test_publication_fails_closed_when_git_status_fails(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        def fail_status(*args, **kwargs):
            if args[0] == "status":
                return subprocess.CompletedProcess(["git", *args], 1, "", "status failed")
            return run_git(*args, **kwargs)

        with patch("pynchy.host.git_ops.worktree_sync.run_git", side_effect=fail_status):
            result = host_create_pr_from_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert "Could not check worktree status" in result["message"]

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
