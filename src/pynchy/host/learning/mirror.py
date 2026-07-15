"""Local mirror support for Obsidian vault mounts."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.

from pynchy.config.settings import get_settings
from pynchy.host.learning.paths import (  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
    LearningPaths,
)
from pynchy.logger import logger
from pynchy.plugins.runtimes.detection import get_runtime


def should_use_vault_mount_mirror() -> bool:
    """Return whether container mounts should use a local vault mirror."""
    if sys.platform != "darwin":
        return False

    try:
        return get_runtime().name == "apple"
    except RuntimeError as exc:
        logger.debug("Skipping vault mirror runtime check", err=str(exc))
        return False


def prepare_vault_mount_root(paths: LearningPaths) -> Path:
    """Prepare and return the host directory to mount as the vault root."""
    if not should_use_vault_mount_mirror():
        return paths.vault_root

    mirror_root = _mirror_root(paths)
    mirror_profile_root = mirror_root / paths.profile_root.relative_to(paths.vault_root)
    mirror_profile_root.mkdir(parents=True, exist_ok=True)
    if paths.profile_root.exists():
        shutil.copytree(
            paths.profile_root,
            mirror_profile_root,
            dirs_exist_ok=True,
            symlinks=True,
        )
    logger.warning(
        "Using mirrored Obsidian vault mount for Apple Container",
        vault_root=str(paths.vault_root),
        mirror_root=str(mirror_root),
        profile=paths.profile_slug,
    )
    return mirror_root


def prepare_full_vault_host_root(paths: LearningPaths) -> Path:
    """Prepare a full vault mirror for an admin agent running directly on the host.

    Apple containers receive only their profile subtree. An admin host run may
    need shared operational notes outside that subtree, but must not depend on
    direct access to the TCC-protected Documents path.
    """
    if not should_use_vault_mount_mirror():
        return paths.vault_root

    mirror_root = get_settings().data_dir / "learning" / "host-vault-mirrors" / paths.profile_slug
    shutil.copytree(paths.vault_root, mirror_root, dirs_exist_ok=True, symlinks=True)
    logger.warning(
        "Using full mirrored Obsidian vault for admin host execution",
        vault_root=str(paths.vault_root),
        mirror_root=str(mirror_root),
        profile=paths.profile_slug,
    )
    return mirror_root


def sync_vault_mount_mirror(paths: LearningPaths) -> None:
    """Copy successful reviewer changes from the local mirror back to the vault."""
    if not should_use_vault_mount_mirror():
        return

    mirror_profile_root = _mirror_root(paths) / paths.profile_root.relative_to(paths.vault_root)
    if not mirror_profile_root.exists():
        return
    paths.profile_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        mirror_profile_root,
        paths.profile_root,
        dirs_exist_ok=True,
        symlinks=True,
    )


def _mirror_root(paths: LearningPaths) -> Path:
    return get_settings().data_dir / "learning" / "vault-mirrors" / paths.profile_slug
