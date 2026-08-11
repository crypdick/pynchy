"""Safe retirement of filesystem artifacts owned by dynamic workspaces."""

from __future__ import annotations

import asyncio
import shutil
import subprocess  # noqa: S404 - result type for injected Git adapter.
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from pynchy.conversation.api import conversation_id_from_folder, parent_workspace_name
from pynchy.host.orchestrator.threads import provider_conversation_exists
from pynchy.logger import logger
from pynchy.plugins.api import Channel  # noqa: TC001 - beartype resolves callbacks.
from pynchy.scheduling.api import ScheduledTask  # noqa: TC001 - beartype resolves tasks.
from pynchy.state.api import get_all_sessions, get_all_tasks, get_in_flight_turns
from pynchy.workspace.api import (  # noqa: TC001 - beartype resolves workspace ownership.
    WorkspaceProfile,
)

_DATA_ARTIFACT_DIRS = ("sessions", "ipc", "env", "approvals")
type GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def _valid_folder(folder: str) -> bool:
    return bool(folder) and Path(folder).name == folder and folder not in {".", ".."}


def _artifact_paths(folder: str, *, data_dir: Path, groups_dir: Path) -> list[Path]:
    return [groups_dir / folder, *(data_dir / name / folder for name in _DATA_ARTIFACT_DIRS)]


def _worktree_paths(folder: str, worktrees_dir: Path) -> list[Path] | None:
    if worktrees_dir.is_symlink():
        logger.warning("Retained workspace artifacts with unsafe worktree root")
        return None
    if not worktrees_dir.exists():
        return []
    if not worktrees_dir.is_dir():
        logger.warning("Retained workspace artifacts with unsafe worktree root")
        return None
    try:
        return sorted(
            repo / folder
            for owner in worktrees_dir.iterdir()
            if owner.is_dir() and not owner.is_symlink()
            for repo in owner.iterdir()
            if repo.is_dir() and not repo.is_symlink()
            if (repo / folder).exists() or (repo / folder).is_symlink()
        )
    except OSError as exc:
        logger.warning("Could not scan workspace worktrees", folder=folder, error=str(exc))
        return None


def _worktree_removal_plan(
    paths: list[Path] | None,
    git: GitRunner,
) -> list[tuple[Path, Path]] | None:
    if paths is None:
        return None
    plan: list[tuple[Path, Path]] = []
    for path in paths:
        if path.is_symlink() or not path.is_dir():
            logger.warning("Retained unsafe workspace worktree path", path=str(path))
            return None
        status = git("status", "--porcelain", "--untracked-files=all", cwd=path)
        if status.returncode != 0 or status.stdout.strip():
            logger.warning(
                "Retained workspace artifacts with unfinished Git work",
                path=str(path),
            )
            return None
        common = git(
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            cwd=path,
        )
        if common.returncode != 0:
            logger.warning("Retained workspace with unreadable Git metadata", path=str(path))
            return None
        common_dir = Path(common.stdout.strip())
        if common_dir.name != ".git" or not common_dir.is_dir():
            logger.warning("Retained workspace with unexpected Git metadata", path=str(path))
            return None
        plan.append((path, common_dir.parent))
    return plan


def _group_has_user_files(group_dir: Path) -> bool:
    """Keep workspace files; only the host-owned logs directory is disposable."""
    if not group_dir.is_dir():
        return False
    try:
        return any(path.name != "logs" for path in group_dir.iterdir())
    except OSError:
        return True


def _artifacts_are_disposable(folder: str, artifacts: list[Path], groups_dir: Path) -> bool:
    for path in artifacts:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            logger.warning("Retained unsafe workspace artifact path", path=str(path))
            return False
    if _group_has_user_files(groups_dir / folder):
        logger.warning(
            "Retained workspace artifacts with user workspace files",
            folder=folder,
        )
        return False
    return True


def cleanup_workspace_artifacts(
    folder: str,
    *,
    data_dir: Path,
    groups_dir: Path,
    worktrees_dir: Path,
    git: GitRunner,
) -> bool:
    """Remove one retired workspace while preserving its Git branch.

    Dirty or untracked work blocks the whole cleanup. Clean worktrees leave their
    branch refs behind, so reopening the conversation reattaches committed work.
    """
    if not _valid_folder(folder):
        logger.warning("Refused invalid workspace artifact folder", folder=folder)
        return False
    artifacts = _artifact_paths(folder, data_dir=data_dir, groups_dir=groups_dir)
    if not _artifacts_are_disposable(folder, artifacts, groups_dir):
        return False
    plan = _worktree_removal_plan(_worktree_paths(folder, worktrees_dir), git)
    if plan is None:
        return False
    for path, repo_root in plan:
        removed = git("worktree", "remove", "--force", str(path.resolve()), cwd=repo_root)
        if removed.returncode != 0:
            logger.warning(
                "Could not remove retired workspace worktree",
                path=str(path),
                error=removed.stderr.strip(),
            )
            return False
    try:
        for path in artifacts:
            if path.is_dir():
                shutil.rmtree(path)
    except OSError as exc:
        logger.warning(
            "Could not remove retired workspace artifacts",
            folder=folder,
            error=str(exc),
        )
        return False
    logger.info("Removed retired workspace artifacts", folder=folder, worktrees=len(plan))
    return True


def _artifact_folders(*, data_dir: Path, groups_dir: Path, worktrees_dir: Path) -> set[str]:
    folders: set[str] = set()
    for root in (groups_dir, *(data_dir / name for name in _DATA_ARTIFACT_DIRS)):
        if root.is_dir() and not root.is_symlink():
            try:
                folders.update(path.name for path in root.iterdir() if path.is_dir())
            except OSError as exc:
                logger.warning(
                    "Could not scan workspace artifact root",
                    path=str(root),
                    error=str(exc),
                )
    if worktrees_dir.is_dir() and not worktrees_dir.is_symlink():
        folders.update(path.name for path in _worktree_paths_by_repo(worktrees_dir))
    return folders


def _worktree_paths_by_repo(worktrees_dir: Path) -> list[Path]:
    try:
        return [
            path
            for owner in worktrees_dir.iterdir()
            if owner.is_dir() and not owner.is_symlink()
            for repo in owner.iterdir()
            if repo.is_dir() and not repo.is_symlink()
            for path in repo.iterdir()
            if path.is_dir() or path.is_symlink()
        ]
    except OSError as exc:
        logger.warning("Could not scan managed workspace artifacts", error=str(exc))
        return []


def cleanup_orphaned_workspace_artifacts(
    protected_folders: set[str],
    *,
    data_dir: Path,
    groups_dir: Path,
    worktrees_dir: Path,
    git: GitRunner,
) -> list[str]:
    """Remove unowned dynamic-thread artifacts left by earlier runtimes.

    Routed conversations use their durable terminal lifecycle instead. This sweep
    handles provider threads whose registration disappeared before cleanup existed.
    """
    candidates = sorted(
        candidate
        for candidate in _artifact_folders(
            data_dir=data_dir,
            groups_dir=groups_dir,
            worktrees_dir=worktrees_dir,
        )
        if candidate not in protected_folders
        and parent_workspace_name(candidate) is not None
        and conversation_id_from_folder(candidate) is None
    )
    return [
        folder
        for folder in candidates
        if cleanup_workspace_artifacts(
            folder,
            data_dir=data_dir,
            groups_dir=groups_dir,
            worktrees_dir=worktrees_dir,
            git=git,
        )
    ]


async def cleanup_startup_workspace_artifacts(  # noqa: PLR0913 - composed filesystem boundary.
    workspaces: Iterable[WorkspaceProfile],
    tasks: Iterable[ScheduledTask],
    active_folders: set[str],
    *,
    data_dir: Path,
    groups_dir: Path,
    worktrees_dir: Path,
    git: GitRunner,
) -> list[str]:
    """Reclaim unowned dynamic artifacts after durable owners are restored."""
    sessions, turns = await asyncio.gather(get_all_sessions(), get_in_flight_turns())
    protected_folders = {
        *(profile.folder for profile in workspaces),
        *sessions,
        *(turn.group_folder for turn in turns),
        *active_folders,
        *(
            folder
            for task in tasks
            if task.status in {"active", "paused"}
            for folder in (task.group_folder, task.bound_group_folder)
            if folder is not None
        ),
    }
    return await asyncio.to_thread(
        cleanup_orphaned_workspace_artifacts,
        protected_folders,
        data_dir=data_dir,
        groups_dir=groups_dir,
        worktrees_dir=worktrees_dir,
        git=git,
    )


async def remove_orphaned_workspace_registrations(  # noqa: PLR0913 - lifecycle callbacks.
    config_folders: set[str],
    runtime_restriction_folders: set[str],
    workspaces: dict[str, WorkspaceProfile],
    channels: list[Channel],
    unregister_fn: Callable[[str], Awaitable[None]],
    retire_fn: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Retire unowned registrations after checking durable and provider ownership."""
    retained_tasks = [task for task in await get_all_tasks() if task.status in {"active", "paused"}]
    task_workspace_identities = {
        identity
        for task in retained_tasks
        for identity in (
            task.group_folder,
            task.chat_jid,
            task.bound_group_folder,
            task.bound_chat_jid,
        )
        if identity is not None
    }
    session_workspace_folders = (await get_all_sessions()).keys()
    for jid, profile in list(workspaces.items()):
        if profile.folder in task_workspace_identities or jid in task_workspace_identities:
            logger.info("Retained owned workspace registration", folder=profile.folder, jid=jid)
            continue
        parent_folder = parent_workspace_name(profile.folder)
        if profile.folder in config_folders or profile.is_admin:
            continue
        if (
            conversation_id_from_folder(profile.folder) is not None
            and profile.folder not in runtime_restriction_folders
        ):
            if retire_fn is not None:
                await retire_fn(profile.folder)
            await unregister_fn(jid)
            logger.info(
                "Removed stale routed workspace registration",
                folder=profile.folder,
                jid=jid,
            )
            continue
        if parent_folder in config_folders:
            try:
                exists = await provider_conversation_exists(channels, jid)
            except Exception as exc:  # noqa: BLE001 - provider uncertainty must retain state.
                logger.warning(
                    "Retained workspace after provider presence check failed",
                    folder=profile.folder,
                    jid=jid,
                    error=type(exc).__name__,
                )
                continue
            if exists is not False:
                continue
            if retire_fn is not None:
                await retire_fn(profile.folder)
            await unregister_fn(jid)
            logger.info(
                "Removed provider-deleted workspace registration",
                folder=profile.folder,
                jid=jid,
            )
            continue
        if profile.folder in session_workspace_folders:
            logger.info("Retained owned workspace registration", folder=profile.folder, jid=jid)
            continue
        if retire_fn is not None:
            await retire_fn(profile.folder)
        await unregister_fn(jid)
        logger.info("Removed orphaned workspace registration", folder=profile.folder, jid=jid)
