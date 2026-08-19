"""Fail-closed worktree selection for direct-host routed conversations."""

from __future__ import annotations

import fcntl
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves routed-worktree annotations at runtime.
    Iterator,  # noqa: TC003 - beartype resolves routed-worktree annotations at runtime.
    Sequence,  # noqa: TC003 - beartype resolves routed-worktree annotations at runtime.
)
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from pynchy.host.git_ops._worktree_models import (
    RoutedHostWorktreeError,
    RoutedHostWorktreeResult,
    WorktreeError,
    WorktreeResult,
)
from pynchy.host.git_ops.repo import (
    RepoContext,  # noqa: TC001 - beartype resolves routed-worktree annotations at runtime.
)
from pynchy.host.git_ops.utils import count_commits, detect_main_branch, run_git
from pynchy.logger import logger


# NOTE: Update docs/usage/worktrees.md § Routed Host Workspaces with behavior changes here.
def resolve_routed_host_worktree_cwd(
    group_folder: str,
    source_cwd: Path,
    repo_contexts: Sequence[RepoContext],
    *,
    recovered: bool,
    ensure_worktree_fn: Callable[[str, RepoContext], WorktreeResult],
) -> RoutedHostWorktreeResult:
    """Resolve one routed host run to its own worktree without trusting a parent branch."""
    source_root, source_common_dir = _git_worktree_identity(
        source_cwd, description="configured host working directory"
    )
    repo_ctx = _select_routed_host_repo(source_common_dir, repo_contexts)

    # Git worktree metadata and remote-tracking refs are shared by every child.
    # Recheck the slot after acquiring the repository-wide lock so waiters never
    # act on the pre-lock filesystem state.
    with _routed_worktree_lock(source_common_dir):
        worktree_path = repo_ctx.worktrees_dir / group_folder
        if worktree_path.is_symlink():
            raise RoutedHostWorktreeError(
                "Routed conversation worktree path is a symbolic link; leave it untouched and "
                "recover it manually."
            )
        if worktree_path.exists():
            _validate_routed_worktree_slot(worktree_path, repo_ctx, group_folder)
        elif recovered:
            _reject_unsafe_legacy_source(source_root, repo_ctx)

        try:
            worktree = ensure_worktree_fn(group_folder, repo_ctx)
        except (OSError, WorktreeError) as exc:
            logger.warning(
                "Failed to prepare routed host worktree",
                group=group_folder,
                slug=repo_ctx.slug,
                error=str(exc),
            )
            raise RoutedHostWorktreeError(
                "Could not prepare the routed conversation's isolated worktree. "
                "Check repository access and retry."
            ) from exc

    relative_cwd = source_cwd.resolve().relative_to(source_root)
    routed_cwd = worktree.path / relative_cwd
    if not routed_cwd.is_dir():
        raise RoutedHostWorktreeError(
            "Configured host working directory is unavailable in the routed worktree."
        )
    return RoutedHostWorktreeResult(routed_cwd, tuple(worktree.notices), repo_ctx.slug)


@contextmanager
def _routed_worktree_lock(git_common_dir: Path) -> Iterator[None]:
    """Serialize routed provisioning that mutates one Git common directory."""
    with _acquire_routed_worktree_lock(git_common_dir):
        yield


def _acquire_routed_worktree_lock(git_common_dir: Path) -> TextIO:
    """Open and exclusively lock the Git-common-dir lock file."""
    try:
        lock_file = (git_common_dir / "pynchy-routed-worktree.lock").open("a", encoding="utf-8")
    except OSError as exc:
        raise RoutedHostWorktreeError(
            "Could not lock the routed conversation's repository worktrees. "
            "Check repository access and retry."
        ) from exc
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    except OSError as exc:
        lock_file.close()
        raise RoutedHostWorktreeError(
            "Could not lock the routed conversation's repository worktrees. "
            "Check repository access and retry."
        ) from exc
    return lock_file


def select_routed_host_repo(source_cwd: Path, repo_contexts: Sequence[RepoContext]) -> RepoContext:
    """Return the one selected repository that owns a routed host CWD."""
    _source_root, source_common_dir = _git_worktree_identity(
        source_cwd, description="configured host working directory"
    )
    return _select_routed_host_repo(source_common_dir, repo_contexts)


def _select_routed_host_repo(
    source_common_dir: Path, repo_contexts: Sequence[RepoContext]
) -> RepoContext:
    """Match a Git common directory to exactly one selected repository."""
    matching_repositories = [
        repo_ctx
        for repo_ctx in repo_contexts
        if _git_worktree_identity(repo_ctx.root, description="configured repository checkout")[1]
        == source_common_dir
    ]
    if len(matching_repositories) != 1:
        raise RoutedHostWorktreeError(
            "Configured host working directory must belong to exactly one selected repository."
        )
    return matching_repositories[0]


def _validate_routed_worktree_slot(
    worktree_path: Path, repo_ctx: RepoContext, group_folder: str
) -> None:
    """Reject a populated routed slot unless it is the expected child worktree."""
    if not worktree_path.is_dir():
        raise RoutedHostWorktreeError(
            "Routed conversation worktree path is occupied by a non-directory; leave it "
            "untouched and recover it manually."
        )
    slot_root, slot_common_dir = _git_worktree_identity(
        worktree_path, description="routed conversation worktree"
    )
    _repo_root, repo_common_dir = _git_worktree_identity(
        repo_ctx.root, description="configured repository checkout"
    )
    if slot_root != worktree_path.resolve() or slot_common_dir != repo_common_dir:
        raise RoutedHostWorktreeError(
            "Routed conversation worktree path belongs to a different checkout; leave it "
            "untouched and recover it manually."
        )
    branch = run_git("branch", "--show-current", cwd=worktree_path)
    expected_branch = f"worktree/{group_folder}"
    if branch.returncode != 0 or branch.stdout.strip() != expected_branch:
        raise RoutedHostWorktreeError(
            "Routed conversation worktree path is on an unexpected branch; leave it "
            "untouched and recover it manually."
        )


def _git_worktree_identity(cwd: Path, *, description: str) -> tuple[Path, Path]:
    """Return Git top level and common directory, or fail closed."""
    try:
        top_level = run_git("rev-parse", "--show-toplevel", cwd=cwd)
        common_dir = run_git("rev-parse", "--git-common-dir", cwd=cwd)
    except OSError as exc:
        raise RoutedHostWorktreeError(f"Could not inspect {description}.") from exc
    if (
        top_level.returncode != 0
        or common_dir.returncode != 0
        or not top_level.stdout.strip()
        or not common_dir.stdout.strip()
    ):
        raise RoutedHostWorktreeError(f"Could not verify {description} as a Git worktree.")

    try:
        root = Path(top_level.stdout.strip()).resolve()
        common = Path(common_dir.stdout.strip())
        if not common.is_absolute():
            common = cwd / common
        return root, common.resolve()
    except OSError as exc:
        raise RoutedHostWorktreeError(f"Could not resolve {description} paths.") from exc


def _reject_unsafe_legacy_source(source_root: Path, repo_ctx: RepoContext) -> None:
    """Block recovery when it would silently abandon a parent worktree's changes."""
    try:
        status = run_git("status", "--porcelain", cwd=source_root)
    except OSError as exc:
        raise RoutedHostWorktreeError(
            "Could not verify the recovered session's inherited source checkout."
        ) from exc
    if status.returncode != 0:
        raise RoutedHostWorktreeError(
            "Could not verify the recovered session's inherited source checkout."
        )
    if status.stdout.strip():
        raise RoutedHostWorktreeError(
            "Recovered routed session inherits uncommitted parent work. Parent work remains "
            "untouched; recover it manually into this conversation's worktree before retrying."
        )

    try:
        main_branch = detect_main_branch(cwd=repo_ctx.root)
        ahead = count_commits(f"origin/{main_branch}..HEAD", cwd=source_root)
    except OSError as exc:
        raise RoutedHostWorktreeError(
            "Could not verify whether the inherited source checkout is ahead of its main branch."
        ) from exc
    if ahead is None:
        raise RoutedHostWorktreeError(
            "Could not verify whether the inherited source checkout is ahead of its main branch."
        )
    if ahead:
        raise RoutedHostWorktreeError(
            "Recovered routed session inherits parent commits ahead of main. Parent work remains "
            "untouched; recover it manually into this conversation's worktree before retrying."
        )
