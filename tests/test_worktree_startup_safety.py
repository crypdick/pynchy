"""Startup recovery behavior for repository worktrees and hooks."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests exercise fixed git-result failure paths.
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import (
    RepoContext,
    WorktreeError,
    install_repo_hooks,
    reconcile_worktrees_at_startup,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class _StartupRuntime:
    home_dir: Path
    project_root: Path
    configured_tokens: dict[str, str | None]


def _repo_context(tmp_path: Path) -> tuple[RepoContext, _StartupRuntime]:
    project_root = tmp_path / "project"
    worktrees_dir = project_root / "worktrees"
    project_root.mkdir()
    return (
        RepoContext(slug="owner/repo", root=project_root, worktrees_dir=worktrees_dir),
        _StartupRuntime(
            home_dir=tmp_path / "home",
            project_root=project_root,
            configured_tokens={},
        ),
    )


def _reconcile(
    repo_context: RepoContext,
    runtime: _StartupRuntime,
    *,
    folders: list[str] | None = None,
    git_runner: object | None = None,
    discovered_token: str | None = None,
) -> None:
    git_patch = (
        patch("pynchy.host.git_ops.worktree.run_git", side_effect=git_runner)
        if git_runner is not None
        else patch(
            "pynchy.host.git_ops.worktree.run_git",
            return_value=subprocess.CompletedProcess([], 0),
        )
    )
    with (
        patch("pynchy.host.git_ops.worktree._runtime.runtime", runtime),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_context",
            return_value=repo_context,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.ensure_repo_cloned",
            return_value=True,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_token",
            return_value=discovered_token,
        ),
        patch("pynchy.host.git_ops.worktree.install_repo_hooks"),
        git_patch,
    ):
        reconcile_worktrees_at_startup({"owner/repo": folders or []})


def test_startup_accepts_a_discovered_repository_token(tmp_path: Path, caplog) -> None:
    repo_context, runtime = _repo_context(tmp_path)

    _reconcile(repo_context, runtime, discovered_token=tmp_path.name)

    assert "No git token for repo" not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CompletedProcess([], 1, "", "installer failed"),
        OSError("installer unavailable"),
    ],
)
def test_hook_installation_failure_isolated_from_startup(
    tmp_path: Path,
    failure: object,
) -> None:
    (tmp_path / "prek.toml").write_text("repos = []")
    installer_patch = (
        patch(
            "pynchy.host.git_ops.worktree._run_hook_installer",
            return_value=failure,
        )
        if isinstance(failure, subprocess.CompletedProcess)
        else patch(
            "pynchy.host.git_ops.worktree._run_hook_installer",
            side_effect=failure,
        )
    )
    with installer_patch:
        install_repo_hooks(tmp_path)


def test_startup_skips_unavailable_repo_without_creating_worktrees(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    with (
        patch("pynchy.host.git_ops.worktree._runtime.runtime", runtime),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_context",
            return_value=repo_context,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.ensure_repo_cloned",
            return_value=False,
        ),
        patch("pynchy.host.git_ops.worktree.ensure_worktree") as ensure,
    ):
        reconcile_worktrees_at_startup({"owner/repo": ["group"]})
    ensure.assert_not_called()


def test_startup_continues_when_one_worktree_cannot_be_created(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    with (
        patch("pynchy.host.git_ops.worktree._runtime.runtime", runtime),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_context",
            return_value=repo_context,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.ensure_repo_cloned",
            return_value=True,
        ),
        patch("pynchy.host.git_ops.worktree.repo_manager.get_repo_token", return_value=None),
        patch("pynchy.host.git_ops.worktree.install_repo_hooks"),
        patch(
            "pynchy.host.git_ops.worktree.run_git", return_value=subprocess.CompletedProcess([], 0)
        ),
        patch(
            "pynchy.host.git_ops.worktree.ensure_worktree",
            side_effect=WorktreeError("worktree creation failed"),
        ) as ensure,
    ):
        reconcile_worktrees_at_startup({"owner/repo": ["group"]})
    ensure.assert_called_once_with("group", repo_context, mark_used=False)


def test_startup_rebase_handles_missing_branch_and_divergence_failure(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    repo_context.worktrees_dir.mkdir(parents=True)
    (repo_context.worktrees_dir / "missing-branch").mkdir()
    (repo_context.worktrees_dir / "unknown-divergence").mkdir()
    with (
        patch("pynchy.host.git_ops.worktree._runtime.runtime", runtime),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_context",
            return_value=repo_context,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.ensure_repo_cloned",
            return_value=True,
        ),
        patch("pynchy.host.git_ops.worktree.repo_manager.get_repo_token", return_value=None),
        patch("pynchy.host.git_ops.worktree.install_repo_hooks"),
        patch(
            "pynchy.host.git_ops.worktree.run_git", return_value=subprocess.CompletedProcess([], 0)
        ),
        patch("pynchy.host.git_ops.worktree.detect_main_branch", return_value="main"),
        patch(
            "pynchy.host.git_ops.worktree._branch_exists",
            side_effect=[False, True],
        ),
        patch("pynchy.host.git_ops.worktree._worktree_divergence", return_value=None),
    ):
        reconcile_worktrees_at_startup({"owner/repo": []})


def test_startup_rebase_leaves_diverged_worktree_when_rebase_conflicts(
    tmp_path: Path,
) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    repo_context.worktrees_dir.mkdir(parents=True)
    (repo_context.worktrees_dir / "diverged").mkdir()
    with (
        patch("pynchy.host.git_ops.worktree._runtime.runtime", runtime),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_context",
            return_value=repo_context,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.ensure_repo_cloned",
            return_value=True,
        ),
        patch("pynchy.host.git_ops.worktree.repo_manager.get_repo_token", return_value=None),
        patch("pynchy.host.git_ops.worktree.install_repo_hooks"),
        patch(
            "pynchy.host.git_ops.worktree.run_git", return_value=subprocess.CompletedProcess([], 0)
        ),
        patch("pynchy.host.git_ops.worktree.detect_main_branch", return_value="main"),
        patch("pynchy.host.git_ops.worktree._branch_exists", return_value=True),
        patch("pynchy.host.git_ops.worktree._worktree_divergence", return_value=(1, 1)),
        patch("pynchy.host.git_ops.worktree._safe_rebase", return_value=False) as rebase,
    ):
        reconcile_worktrees_at_startup({"owner/repo": []})
    rebase.assert_called_once_with("main", cwd=repo_context.worktrees_dir / "diverged")


def test_startup_rebase_aborts_a_conflicting_rebase(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    repo_context.worktrees_dir.mkdir(parents=True)
    (repo_context.worktrees_dir / "diverged").mkdir()
    with (
        patch("pynchy.host.git_ops.worktree._runtime.runtime", runtime),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_context",
            return_value=repo_context,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.ensure_repo_cloned",
            return_value=True,
        ),
        patch("pynchy.host.git_ops.worktree.repo_manager.get_repo_token", return_value=None),
        patch("pynchy.host.git_ops.worktree.install_repo_hooks"),
        patch(
            "pynchy.host.git_ops.worktree.run_git",
            side_effect=[
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, str(tmp_path / "git-dir")),
                subprocess.CompletedProcess([], 1, "", "conflict"),
                subprocess.CompletedProcess([], 0),
            ],
        ) as run_git,
        patch("pynchy.host.git_ops.worktree.detect_main_branch", return_value="main"),
        patch("pynchy.host.git_ops.worktree._branch_exists", return_value=True),
        patch("pynchy.host.git_ops.worktree._worktree_divergence", return_value=(1, 1)),
    ):
        reconcile_worktrees_at_startup({"owner/repo": []})

    assert [call.args[:2] for call in run_git.call_args_list] == [
        ("worktree", "prune"),
        ("rev-parse", "--absolute-git-dir"),
        ("rebase", "main"),
        ("rebase", "--abort"),
    ]


def test_startup_checks_the_configured_repository_token(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    runtime = _StartupRuntime(runtime.home_dir, runtime.project_root, {repo_context.slug: "token"})
    with (
        patch("pynchy.host.git_ops.worktree._runtime.runtime", runtime),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.get_repo_context",
            return_value=repo_context,
        ),
        patch(
            "pynchy.host.git_ops.worktree.repo_manager.ensure_repo_cloned",
            return_value=True,
        ),
        patch("pynchy.host.git_ops.worktree.repo_manager.check_token_expiry") as check_expiry,
        patch("pynchy.host.git_ops.worktree.install_repo_hooks"),
        patch(
            "pynchy.host.git_ops.worktree.run_git", return_value=subprocess.CompletedProcess([], 0)
        ),
    ):
        reconcile_worktrees_at_startup({repo_context.slug: []})

    check_expiry.assert_called_once_with(repo_context.slug, "token")


def test_startup_keeps_old_worktree_when_destination_already_exists(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    old = runtime.home_dir / ".config" / "pynchy" / "worktrees" / "group"
    old.mkdir(parents=True)
    (old / ".git").write_text("gitdir: /tmp/metadata")
    destination = repo_context.worktrees_dir / "group"
    destination.mkdir(parents=True)

    with patch("pynchy.host.git_ops.worktree.run_git") as run_git:
        _reconcile(repo_context, runtime)

    run_git.assert_not_called()
    assert old.is_dir()
    assert destination.is_dir()


def test_hook_installation_accepts_linked_worktree_git_metadata(tmp_path: Path) -> None:
    git_dir = tmp_path / "main-git" / "worktrees" / "group"
    git_dir.mkdir(parents=True)
    (tmp_path / ".git").write_text(f"gitdir: {git_dir}")
    (git_dir / "commondir").write_text("../..")
    (tmp_path / "prek.toml").write_text("repos = []")
    result = subprocess.CompletedProcess([], 0)

    with patch("pynchy.host.git_ops.worktree._run_hook_installer", return_value=result) as install:
        install_repo_hooks(tmp_path)

    install.assert_called_once_with(tmp_path, "prek")


def test_startup_leaves_unrelated_old_worktree_entries_untouched(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    old_base = runtime.home_dir / ".config" / "pynchy" / "worktrees"
    old_base.mkdir(parents=True)
    unrelated_file = old_base / "notes.txt"
    unrelated_file.write_text("operator notes\n")
    unrelated_directory = old_base / "not-a-worktree"
    unrelated_directory.mkdir()

    _reconcile(repo_context, runtime)

    assert unrelated_file.read_text() == "operator notes\n"
    assert unrelated_directory.is_dir()


def test_startup_skips_rebase_when_worktree_divergence_cannot_be_read(tmp_path: Path) -> None:
    repo_context, runtime = _repo_context(tmp_path)
    repo_context.worktrees_dir.mkdir(parents=True)
    (repo_context.worktrees_dir / "group").mkdir()
    unrelated_file = repo_context.worktrees_dir / "not-a-worktree"
    unrelated_file.write_text("operator data\n")

    with (
        patch("pynchy.host.git_ops.worktree._branch_exists", return_value=True),
        patch("pynchy.host.git_ops.worktree.count_commits", return_value=None),
        patch("pynchy.host.git_ops.worktree._safe_rebase") as rebase,
    ):
        _reconcile(repo_context, runtime)

    rebase.assert_not_called()
    assert unrelated_file.read_text() == "operator data\n"
