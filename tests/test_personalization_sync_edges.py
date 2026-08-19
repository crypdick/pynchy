"""Failure and idle outcomes for personalization repository publication."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests mock fixed git argv.
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.host.git_ops.api import run_git, sync_personalization_repo

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed test-only git argv.
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
    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Initial personalization")
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")
    return project, remote


@contextmanager
def _github_origin(repo: Path, remote: Path) -> Iterator[None]:
    _git(repo, "remote", "set-url", "origin", "git@github.com:owner/personalization.git")
    with (
        patch(
            "pynchy.host.git_ops._personalization_target._github_remote_url",
            return_value=str(remote),
        ),
        patch(
            "pynchy.host.git_ops._personalization_target.git_env_with_token",
            return_value={"GH_TOKEN": "redacted"},
        ),
    ):
        yield


def _result(returncode: int = 0, *, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _delegate_git(
    *args: str,
    personalization_root: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_git(*args, cwd=personalization_root, env=env, inherit_env=False)


def test_status_failure_is_reported_as_failed(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "status" and args == ("--porcelain",):
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_unmeasurable_clean_commit_ahead_count_is_failed(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=None),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_clean_local_commits_require_a_current_head(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Local repair")

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=1),
        patch(
            "pynchy.host.git_ops.personalization._clean_current_main_head",
            return_value=None,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_uncommitted_changes_require_a_current_parent_head(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization._current_main_head", return_value=None),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_uncommitted_changes_reject_a_stale_staged_snapshot(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._staged_snapshot_is_current",
            return_value=False,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_uncommitted_changes_revalidate_after_staging(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._validate_personalization_changes",
            side_effect=[True, False],
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_uncommitted_changes_reject_commit_failure(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "-c" and "commit-tree" in args:
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_uncommitted_changes_reject_branch_change_during_snapshot_check(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._current_main_head",
            side_effect=["head-before", "head-after"],
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_uncommitted_changes_reject_missing_commit_head(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "rev-parse" and args[:2] == ("--verify", "refs/heads/main^{commit}"):
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_uncommitted_changes_reject_wrong_branch_during_commit(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "branch" and args == ("--show-current",):
            return _result(stdout="repair\n")
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_clean_local_commit_rejects_status_failure_during_head_check(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")
    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Local repair")
    status_calls = 0

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal status_calls
        if command == "status":
            status_calls += 1
            if status_calls == 2:
                return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=1),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_clean_local_commit_detects_head_change_during_validation(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=1),
        patch(
            "pynchy.host.git_ops.personalization._clean_current_main_head",
            side_effect=["head-before", "head-after"],
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


@pytest.mark.parametrize(
    ("remote_head", "tracking_returncode"),
    [
        ("ref: refs/heads/main\tHEAD\n", 1),
        ("ref: refs/heads/main\tHEAD\nref: refs/heads/dev\tHEAD\n", 0),
    ],
)
def test_publication_target_rejects_untrusted_remote_state(
    tmp_path: Path, remote_head: str, tracking_returncode: int
):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    def git_result(command: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == "remote":
            return _result(stdout="git@github.com:owner/personalization.git\n")
        if command == "check-ref-format":
            return _result(stdout=f"{args[-1]}\n")
        return {
            "branch": _result(stdout="main\n"),
            "symbolic-ref": _result(stdout="refs/remotes/origin/main\n"),
            "ls-remote": _result(stdout=remote_head),
            "rev-parse": _result(tracking_returncode),
        }.get(command, _result(1))

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops._personalization_target.run_personalization_git",
            side_effect=git_result,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_idle_checkout_fetch_failure_is_reported(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "fetch":
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=0),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_idle_checkout_rejects_divergence_from_origin(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "rev-list":
            return _result(stdout="1 0\n")
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=0),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
        patch(
            "pynchy.host.git_ops.personalization._publication_target_is_current",
            return_value=True,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_idle_checkout_rejects_a_changed_remote_default_branch(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=0),
        patch(
            "pynchy.host.git_ops.personalization._publication_target_is_current",
            return_value=False,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_idle_checkout_accepts_a_matching_origin(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "idle"


@pytest.mark.parametrize("failure", ["merge", "validation"])
def test_idle_checkout_reports_update_failure(tmp_path: Path, failure: str):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "rev-list":
            return _result(stdout="0 1\n")
        if command == "merge" and failure == "merge":
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    def validate(_project: Path, _root: Path) -> None:
        if failure == "validation":
            raise ValueError("invalid upstream configuration")

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=0),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
        patch(
            "pynchy.host.git_ops.personalization._publication_target_is_current",
            return_value=True,
        ),
    ):
        assert sync_personalization_repo(project, validate) == "failed"


def test_idle_checkout_reports_divergence_check_failure(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "rev-list":
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=0),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
        patch(
            "pynchy.host.git_ops.personalization._publication_target_is_current",
            return_value=True,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_successful_push_without_rebase_rechecks_idle_checkout(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# local repair\n")
    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Local repair")

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops.personalization.count_commits", return_value=1),
        patch("pynchy.host.git_ops.personalization.push_local_commits", return_value=True),
        patch(
            "pynchy.host.git_ops.personalization._idle_personalization_checkout",
            return_value="idle",
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "idle"


def test_staging_failure_is_reported_without_publishing(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# pending change\n")

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "status":
            return _result(stdout="changed\n")
        if command == "add":
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
        patch("pynchy.host.git_ops.personalization.push_local_commits") as push,
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"
    push.assert_not_called()


def test_index_snapshot_failure_is_reported_without_publishing(tmp_path: Path):
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# pending change\n")

    def git_result(
        command: str,
        *args: str,
        personalization_root: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == "status":
            return _result(stdout="changed\n")
        if command == "write-tree":
            return _result(1)
        return _delegate_git(
            *((command, *args)), personalization_root=personalization_root, env=env
        )

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._personalization_git",
            side_effect=git_result,
        ),
        patch("pynchy.host.git_ops.personalization.push_local_commits") as push,
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"
    push.assert_not_called()
