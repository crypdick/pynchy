"""Local mirror support for Obsidian vault mounts."""

from __future__ import annotations

import shutil
import sys
from collections.abc import (
    Iterator,  # noqa: TC003 - beartype resolves context-manager annotations.
)
from contextlib import contextmanager
from pathlib import Path  # noqa: TC003 - beartype resolves this runtime annotation.

from pynchy.host.learning.paths import (  # beartype resolves this runtime annotation.
    LearningPaths,
    resolve_automation_memory_paths,
)
from pynchy.logger import logger

_use_vault_mount_mirror = False


def configure_vault_mount_mirror(*, enabled: bool) -> None:
    """Select Apple Container mirror behavior at host composition."""
    global _use_vault_mount_mirror  # noqa: PLW0603 - one host process owns one vault mount mode.
    _use_vault_mount_mirror = enabled


def should_use_vault_mount_mirror() -> bool:
    """Return whether container mounts should use a local vault mirror."""
    return sys.platform == "darwin" and _use_vault_mount_mirror


def prepare_vault_mount_root(paths: LearningPaths) -> Path:
    """Prepare and return the host directory to mount as the vault root."""
    if not should_use_vault_mount_mirror():
        return paths.vault_root

    mirror_root = paths.vault_mirror_root
    _copy_vault_subtree_to_mirror(mirror_root, paths.profile_root, paths.vault_root)
    logger.warning(
        "Using mirrored Obsidian vault mount for Apple Container",
        vault_root=str(paths.vault_root),
        mirror_root=str(mirror_root),
        profile=paths.profile_slug,
    )
    return mirror_root


def prepare_full_vault_host_root(paths: LearningPaths) -> Path | None:
    """Return a prepared full vault mirror for an admin host execution.

    Apple containers receive only their profile subtree. An admin host run may
    need shared operational notes outside that subtree, but the launchd
    process must not scan a TCC-protected Documents path. An operator prepares
    this data-owned mirror from an authorized shell.
    """
    if not should_use_vault_mount_mirror():
        return paths.vault_root

    mirror_root = paths.host_vault_mirror_root
    if not mirror_root.is_dir():
        logger.warning(
            "Prepared full Obsidian vault mirror is unavailable for admin host execution",
            mirror_root=str(mirror_root),
            profile=paths.profile_slug,
        )
        return None
    logger.warning(
        "Using prepared full Obsidian vault mirror for admin host execution",
        mirror_root=str(mirror_root),
        profile=paths.profile_slug,
    )
    return mirror_root


def sync_vault_mount_mirror(paths: LearningPaths) -> None:
    """Copy successful reviewer changes from the local mirror back to the vault."""
    if not should_use_vault_mount_mirror():
        return

    mirror_root = paths.vault_mirror_root
    _copy_mirror_subtree_to_vault(mirror_root, paths.profile_root, paths.vault_root)


@contextmanager
def automation_memory_dir(task_id: str) -> Iterator[Path | None]:
    """Yield durable task memory and sync Apple-runtime writes back to Obsidian."""
    paths = resolve_automation_memory_paths(task_id)
    if paths is None:
        yield None
        return
    if not should_use_vault_mount_mirror():
        paths.canonical.mkdir(parents=True, exist_ok=True)
        yield paths.canonical
        return

    if paths.dirty_marker.exists() and paths.mirror.exists():
        paths.canonical.mkdir(parents=True, exist_ok=True)
        shutil.copytree(paths.mirror, paths.canonical, dirs_exist_ok=True, symlinks=True)
    paths.mirror.mkdir(parents=True, exist_ok=True)
    if paths.canonical.exists():
        shutil.copytree(paths.canonical, paths.mirror, dirs_exist_ok=True, symlinks=True)
    paths.dirty_marker.touch()
    try:
        yield paths.mirror
    finally:
        sync_automation_memory(task_id)


def sync_automation_memory(task_id: str) -> None:
    """Flush a dirty Apple-runtime task mirror before recording completion."""
    paths = resolve_automation_memory_paths(task_id)
    if paths is None or not should_use_vault_mount_mirror() or not paths.dirty_marker.exists():
        return
    paths.canonical.mkdir(parents=True, exist_ok=True)
    shutil.copytree(paths.mirror, paths.canonical, dirs_exist_ok=True, symlinks=True)
    paths.dirty_marker.unlink()


def _copy_vault_subtree_to_mirror(mirror_root: Path, source_root: Path, vault_root: Path) -> None:
    mirror_subtree = mirror_root / source_root.relative_to(vault_root)
    mirror_subtree.mkdir(parents=True, exist_ok=True)
    if source_root.exists():
        shutil.copytree(source_root, mirror_subtree, dirs_exist_ok=True, symlinks=True)


def _copy_mirror_subtree_to_vault(mirror_root: Path, target_root: Path, vault_root: Path) -> None:
    mirror_subtree = mirror_root / target_root.relative_to(vault_root)
    if not mirror_subtree.exists():
        return
    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(mirror_subtree, target_root, dirs_exist_ok=True, symlinks=True)
