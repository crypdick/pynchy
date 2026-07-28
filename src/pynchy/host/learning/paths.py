"""Resolve host and container paths for Obsidian-backed learning."""

from __future__ import annotations

import posixpath
import re
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves learning runtime annotations at runtime.
)
from dataclasses import dataclass
from pathlib import Path

_PROFILE_SLUG_PATTERN = re.compile(r"[^a-z0-9_.-]+")
_DYNAMIC_THREAD_DELIMITER = "__thread_"
_VAULT_ROOT_REQUIRED_ERROR = "learning.obsidian.vault_root is required when learning is enabled"
_PROFILE_ROOT_TEMPLATE_ERROR = "learning.obsidian.default_profile_root must be a valid template"
_PATH_OUTSIDE_VAULT_ERROR = "learning paths must stay inside learning.obsidian.vault_root"


@dataclass(frozen=True)
class LearningPaths:
    profile: str
    profile_slug: str
    vault_root: Path
    vault_mount_path: str
    profile_root: Path
    memory_root: Path
    vault_mirror_root: Path
    host_vault_mirror_root: Path
    mounted_profile_root: str
    mounted_memory_root: str


@dataclass(frozen=True)
class LearningPathsRuntime:
    """Resolved learning-path inputs selected by application composition."""

    enabled: bool
    vault_root: str | None
    vault_mount_path: str
    default_profile_root: str
    memory_dir_name: str
    data_dir: Path
    profile_for_workspace: Callable[[str], str | None]


class LearningConfigError(ValueError):
    pass


_runtime = LearningPathsRuntime(
    enabled=False,
    vault_root=None,
    vault_mount_path="/workspace/vault",
    default_profile_root="systems/pynchy/profiles/{profile}",
    memory_dir_name="memory",
    data_dir=Path.cwd(),
    profile_for_workspace=lambda _folder: None,
)


def configure_learning_paths_runtime(runtime: LearningPathsRuntime) -> None:
    """Set the learning path values selected by the application composition root."""
    global _runtime  # noqa: PLW0603 - one host process owns one learning-path configuration.
    _runtime = runtime


def profile_name_for_group(group_folder: str) -> str:
    profile = _runtime.profile_for_workspace(group_folder)
    if profile is None:
        parent_folder, delimiter, _thread = group_folder.partition(_DYNAMIC_THREAD_DELIMITER)
        if delimiter and parent_folder:
            profile = _runtime.profile_for_workspace(parent_folder)
    return profile or "default"


def resolve_learning_paths(
    group_folder: str, *, profile_override: str | None = None
) -> LearningPaths | None:
    runtime = _runtime
    if not runtime.enabled:
        return None

    if not runtime.vault_root:
        raise LearningConfigError(_VAULT_ROOT_REQUIRED_ERROR)

    vault_root = Path(runtime.vault_root).expanduser().resolve()
    profile = (
        profile_override if profile_override is not None else profile_name_for_group(group_folder)
    )
    profile_slug = _profile_slug(profile)

    profile_rel = _render_profile_root(runtime.default_profile_root, profile_slug)
    profile_root = _resolve_under_vault(vault_root, profile_rel)
    memory_root = _resolve_under_vault(vault_root, profile_rel / runtime.memory_dir_name)

    profile_vault_rel = profile_root.relative_to(vault_root)
    memory_vault_rel = memory_root.relative_to(vault_root)
    learning_data_root = runtime.data_dir / "learning"

    mount_path = runtime.vault_mount_path
    return LearningPaths(
        profile=profile,
        profile_slug=profile_slug,
        vault_root=vault_root,
        vault_mount_path=mount_path,
        profile_root=profile_root,
        memory_root=memory_root,
        vault_mirror_root=learning_data_root / "vault-mirrors" / profile_slug,
        host_vault_mirror_root=learning_data_root / "host-vault-mirrors" / profile_slug,
        mounted_profile_root=_mounted_path(mount_path, profile_vault_rel),
        mounted_memory_root=_mounted_path(mount_path, memory_vault_rel),
    )


def _profile_slug(profile: str) -> str:
    slug = _PROFILE_SLUG_PATTERN.sub("-", profile.lower()).strip("._-")
    return slug or "default"


def _render_profile_root(template: str, profile_slug: str) -> Path:
    try:
        return Path(template.format(profile=profile_slug))
    except (IndexError, KeyError, ValueError) as exc:
        raise LearningConfigError(_PROFILE_ROOT_TEMPLATE_ERROR) from exc


def _resolve_under_vault(vault_root: Path, vault_relative_path: Path) -> Path:
    resolved = (vault_root / vault_relative_path).resolve()
    try:
        resolved.relative_to(vault_root)
    except ValueError as exc:
        raise LearningConfigError(_PATH_OUTSIDE_VAULT_ERROR) from exc
    return resolved


def _mounted_path(mount_path: str, vault_relative_path: Path) -> str:
    rel = vault_relative_path.as_posix()
    if rel == ".":
        return mount_path
    return posixpath.join(mount_path.rstrip("/") or "/", rel)
