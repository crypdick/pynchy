"""Resolve host and container paths for Obsidian-backed learning."""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

from pynchy.config.settings import Settings, get_settings

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
    global_skills_root: Path
    profile_root: Path
    memory_root: Path
    vault_mirror_root: Path
    host_vault_mirror_root: Path
    mounted_profile_root: str
    mounted_memory_root: str


class LearningConfigError(ValueError):
    pass


def profile_name_for_group(group_folder: str) -> str:
    return _profile_name_for_group(get_settings(), group_folder)


def _profile_name_for_group(settings: Settings, group_folder: str) -> str:
    workspace = settings.workspaces.get(group_folder)
    if workspace is None:
        parent_folder, delimiter, _thread = group_folder.partition(_DYNAMIC_THREAD_DELIMITER)
        if delimiter and parent_folder:
            workspace = settings.workspaces.get(parent_folder)
    if workspace is None or not workspace.profiles:
        return "default"
    return workspace.profiles[0]


def resolve_learning_paths(
    group_folder: str, *, profile_override: str | None = None
) -> LearningPaths | None:
    settings = get_settings()
    config = settings.learning
    if not config.enabled:
        return None

    obsidian = config.obsidian
    if not obsidian.vault_root:
        raise LearningConfigError(_VAULT_ROOT_REQUIRED_ERROR)

    vault_root = Path(obsidian.vault_root).expanduser().resolve()
    profile = (
        profile_override
        if profile_override is not None
        else _profile_name_for_group(settings, group_folder)
    )
    profile_slug = _profile_slug(profile)

    global_skills_root = _resolve_under_vault(vault_root, Path("systems/pynchy/skills"))
    profile_rel = _render_profile_root(obsidian.default_profile_root, profile_slug)
    profile_root = _resolve_under_vault(vault_root, profile_rel)
    memory_root = _resolve_under_vault(vault_root, profile_rel / obsidian.memory_dir_name)

    profile_vault_rel = profile_root.relative_to(vault_root)
    memory_vault_rel = memory_root.relative_to(vault_root)
    learning_data_root = settings.data_dir / "learning"

    mount_path = obsidian.mount_path
    return LearningPaths(
        profile=profile,
        profile_slug=profile_slug,
        vault_root=vault_root,
        vault_mount_path=mount_path,
        global_skills_root=global_skills_root,
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
