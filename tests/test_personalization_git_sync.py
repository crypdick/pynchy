"""Tests for automatic personalization repository persistence."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests invoke fixed git argv.
from contextlib import contextmanager
from typing import TYPE_CHECKING
from unittest.mock import ANY, patch

import pytest

from pynchy.host.git_ops.api import count_commits, run_git, sync_personalization_repo

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


def _personalization_repo(
    tmp_path: Path,
    *,
    configure_local_identity: bool = True,
) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")

    project = tmp_path / "project"
    repo = project / "data/personalization"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")
    if configure_local_identity:
        _git(repo, "config", "user.name", "Pynchy")
        _git(repo, "config", "user.email", "pynchy@example.com")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "pynchy.toml").write_text("")
    (repo / "litellm.yaml").write_text("model_list: []\n")
    _git(repo, "add", "--all")
    if configure_local_identity:
        _git(repo, "commit", "-m", "Initial personalization")
    else:
        _git(
            repo,
            "-c",
            "user.name=Pynchy",
            "-c",
            "user.email=pynchy@example.com",
            "commit",
            "-m",
            "Initial personalization",
        )
    _git(repo, "push", "-u", "origin", "main")
    _git(repo, "remote", "set-head", "origin", "main")
    return project, remote


@contextmanager
def _github_origin(repo: Path, remote: Path) -> Iterator[None]:
    """Make a local fixture behave like an authenticated GitHub checkout."""
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


def test_commits_and_pushes_valid_personalization_changes(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path, configure_local_identity=False)
    skill = project / "data/personalization/skills/remember-routing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: remember-routing\ndescription: Remember routing.\n---\n"
    )

    with _github_origin(project / "data/personalization", remote):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "pushed"

    assert not _git(project / "data/personalization", "status", "--porcelain").stdout
    assert "Update Pynchy personalization" in _git(remote, "log", "-1", "--format=%s").stdout


def test_publication_commit_uses_fixed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[user]\n\tname = Host Operator\n\temail = operator@example.invalid\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    project, remote = _personalization_repo(tmp_path, configure_local_identity=False)
    skill = project / "data/personalization/skills/fixed-identity"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: fixed-identity\ndescription: Fixed identity.\n---\n"
    )

    with (
        _github_origin(project / "data/personalization", remote),
        patch(
            "pynchy.host.git_ops._personalization_target.git_env_with_token",
            return_value={"GH_TOKEN": "redacted"},
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "pushed"

    identity = _git(remote, "log", "-1", "--format=%an <%ae>|%cn <%ce>").stdout.strip()
    assert identity == "Pynchy <pynchy@localhost>|Pynchy <pynchy@localhost>"


def test_invalid_changes_remain_uncommitted_for_retry(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    changed = project / "data/personalization/pynchy.toml"
    changed.write_text("invalid")

    def invalid(_project: Path, _root: Path) -> object:
        raise ValueError("invalid personalization")

    with _github_origin(project / "data/personalization", remote):
        assert sync_personalization_repo(project, invalid) == "failed"

    assert "pynchy.toml" in _git(project / "data/personalization", "status", "--porcelain").stdout
    _git(project / "data/personalization", "diff", "--cached", "--quiet")


def test_branch_cas_failure_prevents_remote_push(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    (repo / "pynchy.toml").write_text("# valid change\n")

    def reject_branch_cas(
        *args: str,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
        inherit_env: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "update-ref":
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=1,
                stdout="",
                stderr="simulated branch race",
            )
        return run_git(*args, cwd=cwd, timeout=timeout, env=env, inherit_env=inherit_env)

    with (
        _github_origin(repo, remote),
        patch("pynchy.host.git_ops._personalization_target.run_git", side_effect=reject_branch_cas),
        patch("pynchy.host.git_ops.personalization.push_local_commits") as push,
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    push.assert_not_called()
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_rejects_index_changes_after_validation(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    skill = repo / "skills/remember-routing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: remember-routing\ndescription: Remember routing.\n---\n"
    )

    validation_calls = 0

    def validate(_project: Path, personalization_root: Path) -> None:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls != 2:
            return
        config = personalization_root / "pynchy.toml"
        config.write_text("changed after validation began\n")
        _git(personalization_root, "add", "pynchy.toml")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, validate) == "failed"

    assert validation_calls == 2
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_rejects_unstaged_changes_after_validation(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    skill = repo / "skills/remember-routing"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: remember-routing\ndescription: Remember routing.\n---\n"
    )

    validation_calls = 0

    def validate(_project: Path, personalization_root: Path) -> None:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls != 2:
            return
        (personalization_root / "pynchy.toml").write_text("changed after validation began\n")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, validate) == "failed"

    assert validation_calls == 2
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_validation_failure_does_not_log_personalization_contents(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    (project / "data/personalization/pynchy.toml").write_text("invalid")
    private_content = "private personalization value"

    def invalid(_project: Path, _root: Path) -> object:
        raise ValueError(private_content)

    with (
        patch("pynchy.host.git_ops.personalization.logger.warning") as warning,
        _github_origin(project / "data/personalization", remote),
    ):
        assert sync_personalization_repo(project, invalid) == "failed"

    warning.assert_called_once_with(
        "Personalization changes are not valid yet",
        error_type="ValueError",
    )


def test_validates_clean_local_commits_before_pushing(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    (repo / "pynchy.toml").write_text("# committed locally\n")
    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Validated local repair")
    validated: list[tuple[Path, Path]] = []

    def validate(project_root: Path, personalization_root: Path) -> None:
        validated.append((project_root, personalization_root))

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, validate) == "pushed"

    assert validated == [(project, repo), (project, repo)]
    assert _git(remote, "rev-parse", "main").stdout.strip() != remote_head
    assert (
        _git(remote, "rev-parse", "main").stdout.strip()
        == _git(repo, "rev-parse", "HEAD").stdout.strip()
    )


def test_clean_preflight_keeps_host_token_out_of_local_git(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# committed locally\n")
    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Local repair")

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops._personalization_target.git_env_with_token",
            return_value={"GH_TOKEN": "redacted"},
        ),
        patch(
            "pynchy.host.git_ops.personalization.count_commits",
            wraps=count_commits,
        ) as count,
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "pushed"

    assert "GH_TOKEN" not in count.call_args.kwargs["env"]
    assert count.call_args.kwargs["inherit_env"] is False


def test_invalid_clean_local_commits_remain_unpublished(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    (repo / "pynchy.toml").write_text("invalid committed configuration\n")
    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Invalid local repair")

    def invalid(_project: Path, _root: Path) -> object:
        raise ValueError("invalid personalization")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, invalid) == "failed"

    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_revalidates_after_rebase_before_pushing(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# valid local repair\n")
    _git(repo, "add", "pynchy.toml")
    _git(repo, "commit", "-m", "Local repair")

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.name", "Pynchy")
    _git(other, "config", "user.email", "pynchy@example.com")
    (other / "invalid-remote-config").write_text("invalid\n")
    _git(other, "add", "invalid-remote-config")
    _git(other, "commit", "-m", "Invalid remote change")
    _git(other, "push", "origin", "main")
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()

    def validate(_project: Path, personalization_root: Path) -> None:
        if (personalization_root / "invalid-remote-config").exists():
            raise ValueError("invalid remote configuration")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, validate) == "failed"

    assert (repo / "invalid-remote-config").exists()
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


@pytest.mark.parametrize("when", ["fetch", "rebase", "validation"])
def test_rechecks_remote_default_branch_before_personalization_push(
    tmp_path: Path,
    when: str,
) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    (repo / "pynchy.toml").write_text("# local personalization change\n")
    default_changed = False
    validation_calls = 0

    def change_default_branch() -> None:
        nonlocal default_changed
        if default_changed:
            return
        _git(remote, "branch", "master", "main")
        _git(remote, "symbolic-ref", "HEAD", "refs/heads/master")
        default_changed = True

    def validate(_project: Path, _personalization_root: Path) -> None:
        nonlocal validation_calls
        validation_calls += 1
        if when == "validation" and validation_calls == 3:
            change_default_branch()

    def change_default_after_operation(
        *args: str,
        cwd: Path | None = None,
        timeout: int = 30,
        env: dict[str, str] | None = None,
        inherit_env: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = run_git(*args, cwd=cwd, timeout=timeout, env=env, inherit_env=inherit_env)
        if args[0] == when:
            change_default_branch()
        return result

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.utils.run_git",
            side_effect=change_default_after_operation,
        ),
    ):
        assert sync_personalization_repo(project, validate) == "failed"

    assert default_changed
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head
    assert _git(remote, "rev-parse", "master").stdout.strip() == remote_head


def test_rechecks_default_branch_immediately_before_personalization_push(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    (repo / "pynchy.toml").write_text("# local personalization change\n")
    target_checks = 0
    default_changed = False

    def target_is_current(_root: Path, _target: object) -> bool:
        nonlocal target_checks, default_changed
        target_checks += 1
        current = not default_changed
        if target_checks == 2:
            _git(remote, "branch", "master", "main")
            _git(remote, "symbolic-ref", "HEAD", "refs/heads/master")
            default_changed = True
        return current

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops.personalization._publication_target_is_current",
            side_effect=target_is_current,
        ),
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    assert target_checks == 3
    assert default_changed
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head
    assert _git(remote, "rev-parse", "master").stdout.strip() == remote_head


def test_fast_forwards_remote_only_origin_advance(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    local_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.name", "Pynchy")
    _git(other, "config", "user.email", "pynchy@example.com")
    (other / "remote-only-change").write_text("valid\n")
    _git(other, "add", "remote-only-change")
    _git(other, "commit", "-m", "Remote-only change")
    _git(other, "push", "origin", "main")
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    validation_calls = 0

    def validate(_project: Path, personalization_root: Path) -> None:
        nonlocal validation_calls
        validation_calls += 1
        assert (personalization_root / "remote-only-change").read_text() == "valid\n"

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, validate) == "updated"

    assert validation_calls == 1
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == remote_head
    assert _git(repo, "merge-base", "--is-ancestor", local_head, remote_head).returncode == 0
    assert (repo / "remote-only-change").read_text() == "valid\n"
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_rejects_non_main_personalization_branch(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    _git(repo, "checkout", "-b", "repair")
    (repo / "pynchy.toml").write_text("# uncommitted repair\n")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    assert "pynchy.toml" in _git(repo, "status", "--porcelain").stdout


def test_rejects_detached_personalization_head(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    _git(repo, "checkout", "--detach")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_rejects_independent_personalization_without_origin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    repo = project / "data/personalization"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_rejects_independent_personalization_without_origin_main(tmp_path: Path) -> None:
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

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"


def test_rejects_personalization_without_origin_head(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    _git(repo, "remote", "set-head", "origin", "--delete")
    (repo / "pynchy.toml").write_text("# pending local repair\n")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    assert "pynchy.toml" in _git(repo, "status", "--porcelain").stdout
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_rejects_stale_origin_head_that_disagrees_with_remote_default(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    _git(repo, "branch", "master", "origin/main")
    _git(
        repo,
        "update-ref",
        "refs/remotes/origin/master",
        remote_head,
    )
    _git(repo, "checkout", "master")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
    (repo / "pynchy.toml").write_text("# pending local repair\n")

    with _github_origin(repo, remote):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    assert "pynchy.toml" in _git(repo, "status", "--porcelain").stdout
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_rejects_non_github_personalization_origin(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    (repo / "pynchy.toml").write_text("# pending local repair\n")
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


@pytest.mark.parametrize(
    "origin",
    [
        "https://operator@github.com/owner/personalization.git",
        "https://github.com/owner/personalization.git?redirect=elsewhere",
        "https://github.com/owner/personalization.git#fragment",
        "https://github.com/owner/personalization/extra.git",
    ],
)
def test_rejects_unsafe_github_personalization_origins(tmp_path: Path, origin: str) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    _git(repo, "remote", "set-url", "origin", origin)
    (repo / "pynchy.toml").write_text("# pending local repair\n")

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head


def test_skips_generated_personalization_inside_parent_repository(tmp_path: Path) -> None:
    project = tmp_path / "project"
    personalization = project / "data/personalization"
    personalization.mkdir(parents=True)
    _git(project, "init", "--initial-branch=main")

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "skipped"


def test_skips_personalization_submodule(tmp_path: Path) -> None:
    source = tmp_path / "personalization-source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Pynchy")
    _git(source, "config", "user.email", "pynchy@example.com")
    (source / "pynchy.toml").write_text("")
    _git(source, "add", "pynchy.toml")
    _git(source, "commit", "-m", "Initial personalization")

    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "--initial-branch=main")
    _git(project, "config", "user.name", "Pynchy")
    _git(project, "config", "user.email", "pynchy@example.com")
    _git(
        project,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(source),
        "data/personalization",
    )

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "skipped"


def test_skips_linked_personalization_worktree(tmp_path: Path) -> None:
    source = tmp_path / "personalization-source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "config", "user.name", "Pynchy")
    _git(source, "config", "user.email", "pynchy@example.com")
    (source / "pynchy.toml").write_text("")
    _git(source, "add", "pynchy.toml")
    _git(source, "commit", "-m", "Initial personalization")

    project = tmp_path / "project"
    checkout = project / "data/personalization"
    checkout.parent.mkdir(parents=True)
    _git(source, "worktree", "add", "-b", "publication-checkout", str(checkout))

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "skipped"


def test_skips_symlinked_personalization_checkout(tmp_path: Path) -> None:
    source_project, _remote = _personalization_repo(tmp_path)
    project = tmp_path / "symlink-project"
    personalization = project / "data/personalization"
    personalization.parent.mkdir(parents=True)
    personalization.symlink_to(source_project / "data/personalization", target_is_directory=True)

    assert sync_personalization_repo(project, lambda _project, _root: {}) == "skipped"


def test_uses_host_token_for_github_personalization_remote(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    changed = repo / "pynchy.toml"
    changed.write_text("# changed\n")

    def publish(**kwargs: object) -> bool:
        post_rebase_check = kwargs["post_rebase_check"]
        assert callable(post_rebase_check)
        validated_source = kwargs["validated_source"]
        assert callable(validated_source)
        pre_push_check = kwargs["pre_push_check"]
        assert callable(pre_push_check)
        return post_rebase_check() and validated_source() is not None and pre_push_check()

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops._personalization_target.git_env_with_token",
            return_value={"GH_TOKEN": "redacted"},
        ) as auth,
        patch(
            "pynchy.host.git_ops.personalization.push_local_commits",
            side_effect=publish,
        ) as push,
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "pushed"

    auth.assert_called_once_with("owner/personalization", inherit_host_environment=False)
    assert "GH_TOKEN" not in push.call_args.kwargs["local_env"]
    push.assert_called_once_with(
        cwd=repo,
        env={"GH_TOKEN": "redacted"},
        local_env=ANY,
        post_rebase_check=ANY,
        validated_source=ANY,
        pre_push_check=ANY,
        include_diagnostics=False,
        remote=str(remote),
        fetch_refspec="+refs/heads/main:refs/remotes/origin/main",
        main_branch="main",
        expected_head=ANY,
        inherit_env=False,
    )


def test_requires_host_token_before_personalization_remote_calls(tmp_path: Path) -> None:
    project, remote = _personalization_repo(tmp_path)
    repo = project / "data/personalization"
    remote_head = _git(remote, "rev-parse", "main").stdout.strip()
    (repo / "pynchy.toml").write_text("# pending local repair\n")

    with (
        _github_origin(repo, remote),
        patch(
            "pynchy.host.git_ops._personalization_target.git_env_with_token",
            return_value=None,
        ) as auth,
        patch("pynchy.host.git_ops.personalization.push_local_commits") as push,
        patch("pynchy.host.git_ops.personalization.logger.warning") as warning,
    ):
        assert sync_personalization_repo(project, lambda _project, _root: {}) == "failed"

    auth.assert_called_once_with("owner/personalization", inherit_host_environment=False)
    push.assert_not_called()
    warning.assert_called_once_with(
        "Personalization publication requires host GitHub authentication"
    )
    assert _git(remote, "rev-parse", "main").stdout.strip() == remote_head
