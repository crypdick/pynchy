"""Discover profile-scoped learned skills stored in the Obsidian vault."""

from __future__ import annotations

import stat
from pathlib import Path

from pynchy.config import get_settings
from pynchy.host.learning.paths import resolve_learning_paths
from pynchy.logger import logger


def iter_learned_skill_dirs(group_folder: str) -> list[Path]:
    """Return learned skill directories selected from a group's learning profile."""
    paths = resolve_learning_paths(group_folder)
    if paths is None:
        return []

    skills_root = paths.skills_root
    if not skills_root.exists():
        return []
    if not skills_root.is_dir():
        logger.warning(
            "Skipping learned skill",
            path=str(skills_root),
            reason="skills root is not a directory",
        )
        return []

    resolved_root = skills_root.resolve()
    skill_max_bytes = get_settings().learning.skill_max_bytes
    skill_dirs: list[Path] = []

    for candidate in sorted(skills_root.iterdir(), key=lambda path: path.name):
        if not candidate.is_dir():
            continue

        resolved_candidate = candidate.resolve()
        if not _is_under(resolved_candidate, resolved_root):
            logger.warning(
                "Skipping learned skill",
                path=str(candidate),
                reason="outside skills root",
            )
            continue

        if candidate.is_symlink():
            logger.warning(
                "Skipping learned skill",
                path=str(candidate),
                reason="skill directory is a symlink",
            )
            continue

        skill_md = candidate / "SKILL.md"
        # Learned skills follow session_prep.parse_skill_tier's current loader contract:
        # require a real SKILL.md here, but do not invent learned-only frontmatter fields.
        if not _is_regular_file_without_symlink(skill_md):
            logger.warning(
                "Skipping learned skill",
                path=str(candidate),
                reason="SKILL.md is missing, unreadable, or a symlink",
            )
            continue

        size_bytes = _directory_size_bytes(candidate)
        if size_bytes is None:
            logger.warning(
                "Skipping learned skill",
                path=str(candidate),
                reason="contains symlink or unreadable file",
            )
            continue
        if size_bytes > skill_max_bytes:
            logger.warning(
                "Skipping learned skill",
                path=str(candidate),
                reason="exceeds byte budget",
                size_bytes=size_bytes,
                max_bytes=skill_max_bytes,
            )
            continue

        skill_dirs.append(resolved_candidate)

    return skill_dirs


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_regular_file_without_symlink(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _directory_size_bytes(root: Path) -> int | None:
    total = 0
    for path in root.rglob("*"):
        try:
            stat_result = path.lstat()
        except OSError as exc:
            logger.warning(
                "Skipping unreadable learned skill file",
                path=str(path),
                err=str(exc),
            )
            return None

        if stat.S_ISLNK(stat_result.st_mode):
            logger.warning(
                "Skipping learned skill file symlink",
                path=str(path),
            )
            return None
        if stat.S_ISREG(stat_result.st_mode):
            try:
                total += path.stat().st_size
            except OSError as exc:
                logger.warning(
                    "Skipping unreadable learned skill file",
                    path=str(path),
                    err=str(exc),
                )
                return None

    return total
