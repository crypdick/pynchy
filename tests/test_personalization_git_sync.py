"""Tests for automatic personalization repository persistence."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - tests invoke fixed git argv.
from typing import TYPE_CHECKING
from unittest.mock import patch

from pynchy.host.git_ops.api import sync_personalization_repo

if TYPE_CHECKING:
    from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603, RUF100 - fixed test-only git argv.
        ["/usr/bin/git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _personalization_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")

    project = tmp_path / "project"
    repo = project / "data/personalization"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.name", "Pynchy")
    _git(repo, "config", "user.email", "pynchy@example.com")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "pynchy.toml").write_text("")
    (repo / "litellm.yaml").write_text("model_list: []\n")
    _git(repo, "add", "--all")
    _git(repo, "commit", "-m", "Initial personalization")
    _git(repo, "push", "-u", "origin", "main")
    return project, remote


def test_commits_and_pushes_valid_personalization_changes(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    skill = project / "data/personalization/skills/remember-routing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: remember-routing\ndescription: Remember routing.\n---\n"
    )

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "pushed"

    assert not _git(project / "data/personalization", "status", "--porcelain").stdout
    assert "Update Pynchy personalization" in _git(remote, "log", "-1", "--format=%s").stdout


def test_invalid_changes_remain_uncommitted_for_retry(tmp_path: Path) -> None:
    project, _remote = _personalization_repo(tmp_path)
    changed = project / "data/personalization/pynchy.toml"
    changed.write_text("invalid")

    def invalid(_project: Path, _root: Path) -> object:
        raise ValueError("invalid personalization")

    assert sync_personalization_repo(project, invalid) == "failed"

    assert "pynchy.toml" in _git(project / "data/personalization", "status", "--porcelain").stdout


def test_skips_generated_personalization_inside_parent_repository(tmp_path: Path) -> None:
    project = tmp_path / "project"
    personalization = project / "data/personalization"
    personalization.mkdir(parents=True)
    _git(project, "init", "--initial-branch=main")

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "skipped"


def test_uses_host_token_for_github_personalization_remote(tmp_path: Path) -> None:
    project, _remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    _git(repo, "remote", "set-url", "origin", "git@github.com:owner/personalization.git")
    changed = repo / "pynchy.toml"
    changed.write_text("# changed\n")

    with (
        patch(
            "pynchy.host.git_ops.personalization.git_env_with_token",
            return_value={"GH_TOKEN": "redacted"},
        ) as auth,
        patch(
            "pynchy.host.git_ops.personalization.push_local_commits",
            return_value=True,
        ) as push,
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "pushed"

    auth.assert_called_once_with("owner/personalization")
    push.assert_called_once_with(
        cwd=repo,
        env={"GH_TOKEN": "redacted"},
    )
