"""Workspace management operations.

Utilities for renaming and managing workspaces. These operate on the full
set of resources tied to a workspace folder name: database records, filesystem
directories, git worktrees, and IPC state.

IMPORTANT: The service must be stopped before calling rename_workspace().
Running containers reference the old folder name in mounts and IPC paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pynchy.config import DATA_DIR, GROUPS_DIR, PROJECT_ROOT, WORKTREES_DIR
from pynchy.logger import logger

_SUBPROCESS_TIMEOUT = 30


class RenameError(Exception):
    """Workspace rename failed."""


def _run_git(
    *args: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT,
    )


async def rename_workspace(
    old_folder: str,
    new_folder: str,
    new_name: str | None = None,
) -> None:
    """Rename a workspace folder and update all references.

    Updates: registered_groups, scheduled_tasks, sessions (DB),
    group dir, session dir, IPC dir (filesystem), and git worktree + branch.

    Args:
        old_folder: Current folder name
        new_folder: New folder name
        new_name: Optional new display name (if None, keeps existing)

    Raises:
        RenameError: If any critical step fails
    """
    from pynchy.db import _get_db

    db = _get_db()

    # --- Database updates (single transaction) ---
    if new_name:
        await db.execute(
            "UPDATE registered_groups SET folder = ?, name = ? WHERE folder = ?",
            (new_folder, new_name, old_folder),
        )
    else:
        await db.execute(
            "UPDATE registered_groups SET folder = ? WHERE folder = ?",
            (new_folder, old_folder),
        )

    await db.execute(
        "UPDATE scheduled_tasks SET group_folder = ? WHERE group_folder = ?",
        (new_folder, old_folder),
    )
    await db.execute(
        "UPDATE sessions SET group_folder = ? WHERE group_folder = ?",
        (new_folder, old_folder),
    )
    await db.commit()

    # --- Filesystem renames ---
    _rename_dir(GROUPS_DIR / old_folder, GROUPS_DIR / new_folder, "group")
    _rename_dir(
        DATA_DIR / "sessions" / old_folder,
        DATA_DIR / "sessions" / new_folder,
        "sessions",
    )
    _rename_dir(
        DATA_DIR / "ipc" / old_folder,
        DATA_DIR / "ipc" / new_folder,
        "ipc",
    )

    # --- Git worktree ---
    old_worktree = WORKTREES_DIR / old_folder
    new_worktree = WORKTREES_DIR / new_folder
    if old_worktree.exists():
        # git worktree move updates git's internal registry
        result = _run_git("worktree", "move", str(old_worktree), str(new_worktree))
        if result.returncode != 0:
            raise RenameError(f"git worktree move failed: {result.stderr.strip()}")

        # Rename the tracking branch
        old_branch = f"worktree/{old_folder}"
        new_branch = f"worktree/{new_folder}"
        result = _run_git("branch", "-m", old_branch, new_branch)
        if result.returncode != 0:
            logger.warning(
                "Branch rename failed (worktree moved but branch kept old name)",
                old=old_branch,
                new=new_branch,
                error=result.stderr.strip(),
            )

    logger.info("Workspace renamed", old=old_folder, new=new_folder)


def _rename_dir(old: Path, new: Path, label: str) -> None:
    """Rename a directory if it exists. Skip silently if it doesn't."""
    if old.exists():
        if new.exists():
            raise RenameError(f"Target {label} directory already exists: {new}")
        old.rename(new)
        logger.debug(f"{label} directory renamed", old=str(old), new=str(new))
