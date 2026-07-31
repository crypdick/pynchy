"""Public repository resolution and checkout-recovery behavior."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests mock fixed git argv.
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config.api import RepoConfig, ReposConfig
from pynchy.host.git_ops.api import (
    RepoContext,
    ensure_repo_cloned,
    get_repo_context,
    repo_host_root,
    resolve_repos_for_group,
    run_git,
)

SLUG = "owner/project"


@dataclass(frozen=True)
class _Workspace:
    repo: list[str]


def _completed(returncode: int = 0, *, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def _init_repo(path: Path, *, commit: bool = True, origin: str | None = None) -> None:
    path.mkdir(parents=True)
    assert run_git("init", "-b", "main", cwd=path).returncode == 0
    assert run_git("config", "user.email", "tests@example.invalid", cwd=path).returncode == 0
    assert run_git("config", "user.name", "Pynchy Tests", cwd=path).returncode == 0
    if commit:
        (path / "README.md").write_text("ready\n")
        assert run_git("add", "README.md", cwd=path).returncode == 0
        assert run_git("commit", "-m", "initial", cwd=path).returncode == 0
    if origin is not None:
        assert run_git("remote", "add", "origin", origin, cwd=path).returncode == 0


def _repo_context(tmp_path: Path) -> RepoContext:
    return RepoContext(SLUG, tmp_path / "repo", tmp_path / "worktrees")


def test_get_repo_context_returns_none_when_host_root_is_unavailable():
    settings = make_settings()
    with (
        patch("pynchy.config.api.get_settings", return_value=settings),
        patch("pynchy.host.git_ops.repo.repo_host_root", return_value=None),
    ):
        assert get_repo_context(SLUG) is None


def test_repo_host_root_rejects_malformed_slug():
    assert repo_host_root(make_settings(), "not-a-slug") is None


def test_repo_host_root_honors_explicit_path_override(tmp_path: Path):
    checkout = tmp_path / "operator-checkout"
    settings = make_settings(
        repos=ReposConfig(
            root=tmp_path / "repos",
            overrides={SLUG: RepoConfig(path=str(checkout))},
        )
    )
    assert repo_host_root(settings, SLUG) == checkout


def test_invalid_explicit_path_is_not_cloned(tmp_path: Path):
    checkout = tmp_path / "operator-checkout"
    checkout.write_text("operator data\n")
    settings = make_settings(
        repos=ReposConfig(
            root=tmp_path / "repos",
            overrides={SLUG: RepoConfig(path=str(checkout))},
        )
    )
    with (
        patch("pynchy.config.api.get_settings", return_value=settings),
        patch("pynchy.host.git_ops.repo._clone_repo_to") as clone,
    ):
        assert ensure_repo_cloned(_repo_context(tmp_path)) is False
    clone.assert_not_called()
    assert checkout.read_text() == "operator data\n"


def test_uncommitted_repository_without_head_is_rejected(tmp_path: Path):
    checkout = tmp_path / "operator-checkout"
    _init_repo(checkout, commit=False, origin=f"https://github.com/{SLUG}")
    settings = make_settings(
        repos=ReposConfig(
            root=tmp_path / "repos",
            overrides={SLUG: RepoConfig(path=str(checkout))},
        )
    )
    with patch("pynchy.config.api.get_settings", return_value=settings):
        assert ensure_repo_cloned(_repo_context(tmp_path)) is False


def test_repository_without_origin_is_rejected(tmp_path: Path):
    checkout = tmp_path / "operator-checkout"
    _init_repo(checkout)
    settings = make_settings(
        repos=ReposConfig(
            root=tmp_path / "repos",
            overrides={SLUG: RepoConfig(path=str(checkout))},
        )
    )
    with patch("pynchy.config.api.get_settings", return_value=settings):
        assert ensure_repo_cloned(_repo_context(tmp_path)) is False


def test_origin_without_owner_and_repo_is_rejected(tmp_path: Path):
    checkout = tmp_path / "operator-checkout"
    _init_repo(checkout, origin="https://github.com/owner-only.git")
    settings = make_settings(
        repos=ReposConfig(
            root=tmp_path / "repos",
            overrides={SLUG: RepoConfig(path=str(checkout))},
        )
    )
    with patch("pynchy.config.api.get_settings", return_value=settings):
        assert ensure_repo_cloned(_repo_context(tmp_path)) is False


@pytest.mark.parametrize(
    "origin",
    [
        "https://[invalid",
        "https://user@github.com/owner/project.git",
        "ssh://other@github.com/owner/project.git",
        "git://github.com/owner/project.git",
        "https://github.com/owner/project.git?token=embedded",
    ],
)
def test_unsafe_github_origins_are_rejected(tmp_path: Path, origin: str):
    checkout = tmp_path / "operator-checkout"
    _init_repo(checkout, origin=origin)
    settings = make_settings(
        repos=ReposConfig(
            root=tmp_path / "repos",
            overrides={SLUG: RepoConfig(path=str(checkout))},
        )
    )

    with patch("pynchy.config.api.get_settings", return_value=settings):
        assert ensure_repo_cloned(_repo_context(tmp_path)) is False


def test_clone_remote_normalization_failure_cleans_staged_checkout(tmp_path: Path):
    repo_ctx = _repo_context(tmp_path)
    calls: list[list[str]] = []

    def run_clone(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "clone":
            Path(cmd[3]).mkdir()
            return _completed()
        return _completed(1, stderr="remote setup failed")

    with patch("pynchy.host.git_ops.utils._run_git_process", side_effect=run_clone):
        assert ensure_repo_cloned(repo_ctx) is False

    assert len(calls) == 2
    assert not list(tmp_path.glob(".repo.pynchy-clone-*"))


def test_clone_readiness_failure_cleans_staged_checkout(tmp_path: Path):
    repo_ctx = _repo_context(tmp_path)

    def run_clone(cmd, **kwargs):
        if cmd[1] == "clone":
            Path(cmd[3]).mkdir()
            return _completed()
        if cmd[1:3] == ["rev-parse", "--show-toplevel"]:
            return _completed(1, stderr="not a worktree")
        return _completed()

    with patch("pynchy.host.git_ops.utils._run_git_process", side_effect=run_clone):
        assert ensure_repo_cloned(repo_ctx) is False

    assert not list(tmp_path.glob(".repo.pynchy-clone-*"))


def test_failed_staged_publish_cleans_verified_checkout(tmp_path: Path):
    """A verified clone is removed if publishing it into place fails."""
    repo_ctx = _repo_context(tmp_path)
    staged_roots: list[Path] = []

    def clone_ready(_repo_ctx: RepoContext, target: Path) -> bool:
        target.mkdir()
        staged_roots.append(target)
        return True

    with (
        patch("pynchy.host.git_ops.repo._clone_repo_to", side_effect=clone_ready),
        patch("pynchy.host.git_ops.repo._publish_staged_checkout", return_value=False),
    ):
        assert ensure_repo_cloned(repo_ctx) is False

    assert len(staged_roots) == 1
    assert not staged_roots[0].exists()


def test_empty_workspace_resolution_returns_no_repositories():
    with patch("pynchy.host.git_ops.repo.load_resolved_config", return_value=None):
        assert resolve_repos_for_group("group") == []


def test_workspace_resolution_preserves_configured_repo_order(tmp_path: Path):
    settings = make_settings(
        repos=ReposConfig(root=tmp_path / "repos"),
    )
    with (
        patch("pynchy.config.api.get_settings", return_value=settings),
        patch(
            "pynchy.host.git_ops.repo.load_resolved_config",
            return_value=_Workspace(["owner/first", "owner/second"]),
        ),
    ):
        resolved = resolve_repos_for_group("group")

    assert [repo.slug for repo in resolved] == ["owner/first", "owner/second"]
