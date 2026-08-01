"""Public worktree-registration guards for coordinated Git sync."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures only.
from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.host.git_ops import sync_poll
from pynchy.host.git_ops.api import RepoContext, ensure_worktree, host_sync_worktree

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - test helper runs fixed git argv against temp repos
        ["git", *args],  # noqa: S607 - test helper resolves git from PATH
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def git_env(tmp_path: Path):
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main")
    clone = tmp_path / "setup-clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "config", "user.email", "test@test.com")
    _git(clone, "config", "user.name", "Test")
    (clone / "README.md").write_text("initial")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "initial commit")
    _git(clone, "push", "origin", "main")
    project = tmp_path / "project"
    _git(tmp_path, "clone", str(origin), str(project))
    _git(project, "config", "user.email", "test@test.com")
    _git(project, "config", "user.name", "Test")
    worktrees_dir = tmp_path / "worktrees"
    settings = make_settings(project_root=project, worktrees_dir=worktrees_dir)
    repo_ctx = RepoContext(slug="owner/pynchy", root=project, worktrees_dir=worktrees_dir)
    with ExitStack() as stack:
        stack.enter_context(patch("pynchy.host.git_ops.utils._default_cwd", settings.project_root))
        sync_poll.configure_git_sync_runtime(
            sync_poll.GitSyncRuntime(
                project_root=settings.project_root,
                repo_slugs=tuple(settings.repos.overrides),
                get_restart_hash=lambda: "test-config",
            )
        )
        yield {
            "origin": origin,
            "project": project,
            "worktrees_dir": worktrees_dir,
            "repo_ctx": repo_ctx,
        }


class TestHostSyncWorktreeRegistration:
    def test_sync_blocks_an_unregistered_worktree(self, git_env: dict):
        repo_ctx = git_env["repo_ctx"]
        ensure_worktree("agent-1", repo_ctx)

        with patch(
            "pynchy.host.git_ops.worktree_sync._is_registered_worktree",
            return_value=False,
        ):
            result = host_sync_worktree("agent-1", repo_ctx)

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
            result = host_sync_worktree("agent-1", repo_ctx)

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
            result = host_sync_worktree("agent-1", repo_ctx)

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
            result = host_sync_worktree("agent-1", repo_ctx)

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
            result = host_sync_worktree("agent-1", repo_ctx)

        assert result["success"] is False
        assert "not registered" in result["message"]
