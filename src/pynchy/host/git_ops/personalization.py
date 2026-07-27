"""Automatic persistence for the independent personalization repository."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
    Callable,
)
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.

from pynchy.host.git_ops.repo import github_slug_from_origin
from pynchy.host.git_ops.utils import (
    count_unpushed_commits,
    git_env_with_token,
    push_local_commits,
    redact_git_diagnostic,
    run_git,
)
from pynchy.host.paths import PERSONALIZATION_RELATIVE_DIR
from pynchy.logger import logger

_COMMIT_MESSAGE = "Update Pynchy personalization"


def sync_personalization_repo(
    project_root: Path,
    validator: Callable[[Path, Path], object],
) -> str:
    """Validate, commit, and push pending personalization changes."""
    personalization_root = project_root / PERSONALIZATION_RELATIVE_DIR
    if not _is_independent_git_repo(personalization_root):
        return "skipped"

    status = run_git("status", "--porcelain", cwd=personalization_root)
    if status.returncode != 0:
        _log_failure("status", status.stderr)
        return "failed"

    changed = bool(status.stdout.strip())
    if changed and not _commit_personalization_changes(
        project_root,
        personalization_root,
        validator,
    ):
        return "failed"

    had_unpushed_commits = changed or count_unpushed_commits(cwd=personalization_root) > 0
    if not had_unpushed_commits:
        return "idle"
    if not push_local_commits(
        cwd=personalization_root,
        env=_git_auth_env(personalization_root),
    ):
        return "failed"
    logger.info("Pushed personalization repository changes")
    return "pushed"


def _is_independent_git_repo(personalization_root: Path) -> bool:
    if not personalization_root.is_dir():
        return False
    probe = run_git("rev-parse", "--show-toplevel", cwd=personalization_root)
    return (
        probe.returncode == 0
        and bool(probe.stdout.strip())
        and Path(probe.stdout.strip()).resolve() == personalization_root.resolve()
    )


def _git_auth_env(personalization_root: Path) -> dict[str, str] | None:
    origin = run_git("remote", "get-url", "origin", cwd=personalization_root)
    slug = github_slug_from_origin(origin.stdout) if origin.returncode == 0 else None
    return git_env_with_token(slug) if slug is not None else None


def _commit_personalization_changes(
    project_root: Path,
    personalization_root: Path,
    validator: Callable[[Path, Path], object],
) -> bool:
    try:
        validator(project_root, personalization_root)
    except (OSError, ValueError) as exc:
        logger.warning("Personalization changes are not valid yet", error=str(exc))
        return False

    add = run_git("add", "--all", cwd=personalization_root)
    if add.returncode != 0:
        _log_failure("add", add.stderr)
        return False

    staged = run_git("diff", "--cached", "--quiet", cwd=personalization_root)
    if staged.returncode == 0:
        return True
    if staged.returncode != 1:
        _log_failure("diff", staged.stderr)
        return False

    commit = run_git("commit", "-m", _COMMIT_MESSAGE, cwd=personalization_root)
    if commit.returncode != 0:
        _log_failure("commit", commit.stderr)
        return False
    logger.info("Committed personalization repository changes")
    return True


def _log_failure(action: str, stderr: str) -> None:
    logger.warning(
        "Could not persist personalization repository",
        action=action,
        error=redact_git_diagnostic(stderr),
    )
