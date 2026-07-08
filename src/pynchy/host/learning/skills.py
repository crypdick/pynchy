"""Discover profile-scoped learned skills stored in the Obsidian vault."""

from __future__ import annotations

import stat
from pathlib import Path

from pynchy.config import get_settings
from pynchy.host.learning.paths import resolve_learning_paths
from pynchy.logger import logger


def _sorted_skill_candidates(skills_root: Path) -> list[Path] | None:
    try:
        return sorted(skills_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        logger.warning(
            "Skipping learned skills root",
            path=str(skills_root),
            reason="unable to list skills root",
            err=str(exc),
        )
        return None


def _skip_learned_skill(candidate: Path, reason: str, **fields: object) -> None:
    logger.warning(
        "Skipping learned skill",
        path=str(candidate),
        reason=reason,
        **fields,
    )


def _validated_skill_dir(
    candidate: Path, *, resolved_root: Path, skill_max_bytes: int
) -> Path | None:
    if not candidate.is_dir():
        return None

    resolved_candidate = candidate.resolve()
    if not _is_under(resolved_candidate, resolved_root):
        _skip_learned_skill(candidate, "outside skills root")
        return None
    if candidate.is_symlink():
        _skip_learned_skill(candidate, "skill directory is a symlink")
        return None

    skill_md = candidate / "SKILL.md"
    # Learned skills follow session_prep.parse_skill_tier's current loader contract:
    # require a real SKILL.md here, but do not invent learned-only frontmatter fields.
    if not _is_regular_file_without_symlink(skill_md):
        _skip_learned_skill(candidate, "SKILL.md is missing, unreadable, or a symlink")
        return None

    size_bytes = _directory_size_bytes(candidate)
    if size_bytes is None:
        _skip_learned_skill(candidate, "contains symlink or unreadable file")
        return None
    if size_bytes > skill_max_bytes:
        _skip_learned_skill(
            candidate,
            "exceeds byte budget",
            size_bytes=size_bytes,
            max_bytes=skill_max_bytes,
        )
        return None
    return resolved_candidate


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

    candidates = _sorted_skill_candidates(skills_root)
    if candidates is None:
        return []

    for candidate in candidates:
        skill_dir = _validated_skill_dir(
            candidate,
            resolved_root=resolved_root,
            skill_max_bytes=skill_max_bytes,
        )
        if skill_dir is not None:
            skill_dirs.append(skill_dir)

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
    try:
        children = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        logger.warning(
            "Skipping unreadable learned skill directory",
            path=str(root),
            err=str(exc),
        )
        return None

    for path in children:
        try:
            stat_result = path.lstat()
        except OSError as exc:
            logger.warning(
                "Skipping unreadable learned skill file",
                path=str(path),
                err=str(exc),
            )
            return None

        if stat.S_ISDIR(stat_result.st_mode):
            logger.warning(
                "Skipping learned skill nested directory",
                path=str(path),
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
