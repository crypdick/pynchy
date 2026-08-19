"""Automatic persistence for the independent personalization repository."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves this runtime annotation.
    Callable,
)
from pathlib import Path  # beartype resolves this runtime annotation.

from pynchy.host.git_ops._personalization_target import (
    PublicationTarget as _PublicationTarget,
)
from pynchy.host.git_ops._personalization_target import (
    publication_target_is_current as _publication_target_is_current,
)
from pynchy.host.git_ops._personalization_target import (
    resolve_publication_target as _publication_target,
)
from pynchy.host.git_ops._personalization_target import (
    run_personalization_git as _personalization_git,
)
from pynchy.host.git_ops.utils import (
    count_commits,
    git_env_without_credentials,
    push_local_commits,
)
from pynchy.host.paths import PERSONALIZATION_RELATIVE_DIR
from pynchy.logger import logger

_COMMIT_MESSAGE = "Update Pynchy personalization"
_COMMITTER_NAME = "Pynchy"
_COMMITTER_EMAIL = "pynchy@localhost"


def sync_personalization_repo(
    project_root: Path,
    validator: Callable[[Path, Path], object],
) -> str:
    """Validate, commit, and push pending personalization changes."""
    personalization_root = project_root / PERSONALIZATION_RELATIVE_DIR
    if not _is_independent_git_repo(personalization_root):
        return "skipped"
    return _sync_independent_personalization_repo(project_root, personalization_root, validator)


def _sync_independent_personalization_repo(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
) -> str:
    target = _publication_target(personalization_root)
    if target is None:
        return "failed"

    status = _personalization_git(
        "status",
        "--porcelain",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    if status.returncode != 0:
        _log_failure("status")
        return "failed"

    changed = bool(status.stdout.strip())
    if changed:
        return _commit_and_publish_personalization_changes(
            project_root, personalization_root, validator, target
        )
    return _publish_clean_personalization_commits(
        project_root, personalization_root, validator, target
    )


def _commit_and_publish_personalization_changes(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
    target: _PublicationTarget,
) -> str:
    # Validate before staging so invalid user edits leave the index untouched.
    if not _validate_personalization_changes(project_root, personalization_root, validator):
        return "failed"
    source = _commit_personalization_changes(project_root, personalization_root, validator, target)
    if source is None:
        return "failed"
    return _push_personalization_commits(
        project_root, personalization_root, validator, target, source
    )


def _publish_clean_personalization_commits(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
    target: _PublicationTarget,
) -> str:
    ahead = count_commits(
        f"origin/{target.main}..HEAD",
        cwd=personalization_root,
        env=git_env_without_credentials(),
        inherit_env=False,
    )
    if ahead is None:
        logger.warning("Could not determine pending personalization commits")
        return "failed"
    if ahead == 0:
        return _idle_personalization_checkout(
            project_root,
            personalization_root,
            validator,
            target,
        )
    source = _clean_current_main_head(personalization_root, target)
    if source is None:
        return "failed"
    if not _validate_personalization_changes(project_root, personalization_root, validator):
        return "failed"
    if _clean_current_main_head(personalization_root, target) != source:
        _log_failure("working tree")
        return "failed"
    return _push_personalization_commits(
        project_root, personalization_root, validator, target, source
    )


def _push_personalization_commits(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
    target: _PublicationTarget,
    expected_head: str,
) -> str:
    publish_attempted = False

    def canonical_default_is_current() -> bool:
        return _publication_target_is_current(personalization_root, target)

    def validated_rebased_source() -> str | None:
        nonlocal publish_attempted
        publish_attempted = True
        if not _validate_personalization_changes(
            project_root,
            personalization_root,
            validator,
        ):
            return None
        source = _current_main_head(personalization_root, target)
        if source is None or not canonical_default_is_current():
            return None
        return source

    if not push_local_commits(
        cwd=personalization_root,
        env=target.env,
        local_env=git_env_without_credentials(),
        post_rebase_check=canonical_default_is_current,
        validated_source=validated_rebased_source,
        pre_push_check=canonical_default_is_current,
        include_diagnostics=False,
        remote=target.remote_url,
        fetch_refspec=f"+refs/heads/{target.main}:refs/remotes/origin/{target.main}",
        main_branch=target.main,
        expected_head=expected_head,
        inherit_env=False,
    ):
        return "failed"
    if not publish_attempted:
        return _idle_personalization_checkout(
            project_root,
            personalization_root,
            validator,
            target,
        )
    logger.info("Pushed personalization repository changes")
    return "pushed"


def _idle_personalization_checkout(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
    target: _PublicationTarget,
) -> str:
    fetch = _personalization_git(
        "fetch",
        target.remote_url,
        f"+refs/heads/{target.main}:refs/remotes/origin/{target.main}",
        personalization_root=personalization_root,
        env=target.env,
    )
    if fetch.returncode != 0:
        _log_failure("fetch")
        return "failed"
    if not _publication_target_is_current(personalization_root, target):
        return "failed"
    divergence = _personalization_git(
        "rev-list",
        "--left-right",
        "--count",
        f"HEAD...origin/{target.main}",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    counts = divergence.stdout.split()
    if divergence.returncode != 0 or len(counts) != 2:
        logger.warning("Personalization checkout does not match origin main")
        return "failed"
    local_ahead, remote_ahead = counts
    if local_ahead != "0":
        logger.warning("Personalization checkout does not match origin main")
        return "failed"
    if remote_ahead == "0":
        return "idle"
    return _fast_forward_personalization_checkout(
        project_root,
        personalization_root,
        validator,
        target,
    )


def _fast_forward_personalization_checkout(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
    target: _PublicationTarget,
) -> str:
    # Clean upstream commits are desired state on every deployment, including Kubernetes.
    fast_forward = _personalization_git(
        "merge",
        "--ff-only",
        f"origin/{target.main}",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    if fast_forward.returncode != 0:
        _log_failure("fast-forward")
        return "failed"
    if not _validate_personalization_changes(project_root, personalization_root, validator):
        return "failed"
    logger.info("Updated personalization repository from origin")
    return "updated"


def _is_independent_git_repo(personalization_root: Path) -> bool:
    if not personalization_root.is_dir() or personalization_root.is_symlink():
        return False
    probe = _personalization_git(
        "rev-parse", "--show-toplevel", personalization_root=personalization_root
    )
    if not (
        probe.returncode == 0
        and bool(probe.stdout.strip())
        and Path(probe.stdout.strip()).resolve() == personalization_root.resolve()
    ):
        return False
    superproject = _personalization_git(
        "rev-parse",
        "--show-superproject-working-tree",
        personalization_root=personalization_root,
    )
    if superproject.returncode != 0 or superproject.stdout.strip():
        return False
    git_dir = _personalization_git(
        "rev-parse", "--git-dir", personalization_root=personalization_root
    )
    common_dir = _personalization_git(
        "rev-parse", "--git-common-dir", personalization_root=personalization_root
    )
    return (
        git_dir.returncode == 0
        and common_dir.returncode == 0
        and bool(git_dir.stdout.strip())
        and bool(common_dir.stdout.strip())
        and _resolve_git_path(personalization_root, git_dir.stdout)
        == _resolve_git_path(personalization_root, common_dir.stdout)
    )


def _resolve_git_path(personalization_root: Path, raw_path: str) -> Path:
    path = Path(raw_path.strip())
    return path.resolve() if path.is_absolute() else (personalization_root / path).resolve()


def _commit_personalization_changes(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
    target: _PublicationTarget,
) -> str | None:
    staged_snapshot = _stage_personalization_changes(personalization_root)
    if staged_snapshot is None:
        return None
    parent = _current_main_head(personalization_root, target)
    if parent is None:
        return None
    if not _staged_snapshot_is_current(personalization_root, staged_snapshot, target, parent):
        return None
    if not _validate_personalization_changes(project_root, personalization_root, validator):
        return None
    if not _staged_snapshot_is_current(personalization_root, staged_snapshot, target, parent):
        return None
    return _commit_staged_personalization_changes(
        personalization_root,
        target,
        tree=staged_snapshot,
        parent=parent,
    )


def _stage_personalization_changes(personalization_root: Path) -> str | None:
    add = _personalization_git(
        "add", "--all", personalization_root=personalization_root, env=git_env_without_credentials()
    )
    if add.returncode != 0:
        _log_failure("add")
        return None

    staged_snapshot = _personalization_git(
        "write-tree", personalization_root=personalization_root, env=git_env_without_credentials()
    )
    if staged_snapshot.returncode != 0:
        _log_failure("index")
        return None
    return staged_snapshot.stdout.strip()


def _staged_snapshot_is_current(
    personalization_root: Path,
    staged_snapshot: str,
    target: _PublicationTarget,
    expected_head: str,
) -> bool:
    current_snapshot = _personalization_git(
        "write-tree", personalization_root=personalization_root, env=git_env_without_credentials()
    )
    if current_snapshot.returncode != 0 or current_snapshot.stdout.strip() != staged_snapshot:
        _log_failure("index")
        return False
    unstaged = _personalization_git(
        "diff",
        "--no-ext-diff",
        "--quiet",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    untracked = _personalization_git(
        "ls-files",
        "--others",
        "--exclude-standard",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    if unstaged.returncode != 0 or untracked.returncode != 0 or untracked.stdout.strip():
        _log_failure("working tree")
        return False
    if _current_main_head(personalization_root, target) != expected_head:
        _log_failure("branch")
        return False
    return True


def _commit_staged_personalization_changes(
    personalization_root: Path,
    target: _PublicationTarget,
    *,
    tree: str,
    parent: str,
) -> str | None:
    commit = _personalization_git(
        "-c",
        f"user.name={_COMMITTER_NAME}",
        "-c",
        f"user.email={_COMMITTER_EMAIL}",
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        _COMMIT_MESSAGE,
        personalization_root=personalization_root,
        env=git_env_without_credentials(include_identity=False),
    )
    source = commit.stdout.strip() if commit.returncode == 0 else ""
    if not source:
        _log_failure("commit")
        return None
    update_ref = _personalization_git(
        "update-ref",
        "--no-deref",
        f"refs/heads/{target.main}",
        source,
        parent,
        personalization_root=personalization_root,
        env=git_env_without_credentials(include_identity=False),
    )
    if update_ref.returncode != 0:
        _log_failure("branch")
        return None
    logger.info("Committed personalization repository changes")
    return source


def _current_main_head(
    personalization_root: Path,
    target: _PublicationTarget,
) -> str | None:
    branch = _personalization_git(
        "branch",
        "--show-current",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    if branch.returncode != 0 or branch.stdout.strip() != target.main:
        _log_failure("branch")
        return None
    head = _personalization_git(
        "rev-parse",
        "--verify",
        f"refs/heads/{target.main}^{{commit}}",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    if head.returncode != 0 or not head.stdout.strip():
        _log_failure("branch")
        return None
    return head.stdout.strip()


def _clean_current_main_head(
    personalization_root: Path,
    target: _PublicationTarget,
) -> str | None:
    status = _personalization_git(
        "status",
        "--porcelain",
        personalization_root=personalization_root,
        env=git_env_without_credentials(),
    )
    if status.returncode != 0 or status.stdout.strip():
        _log_failure("working tree")
        return None
    return _current_main_head(personalization_root, target)


def _validate_personalization_changes(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
) -> bool:
    try:
        validator(project_root, personalization_root)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Personalization changes are not valid yet",
            error_type=type(exc).__name__,
        )
        return False
    return True


def _log_failure(action: str) -> None:
    logger.warning(
        "Could not persist personalization repository",
        action=action,
    )
