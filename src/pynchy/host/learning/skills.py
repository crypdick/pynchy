"""Discover profile-scoped learned skills stored in the Obsidian vault."""

from __future__ import annotations

import stat
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.

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
    rejection = _skill_dir_rejection(candidate, resolved_candidate, resolved_root, skill_max_bytes)
    if rejection is not None:
        reason, fields = rejection
        _skip_learned_skill(candidate, reason, **fields)
        return None
    return resolved_candidate


def _skill_dir_rejection(
    candidate: Path,
    resolved_candidate: Path,
    resolved_root: Path,
    skill_max_bytes: int,
) -> tuple[str, dict[str, object]] | None:
    if not _is_under(resolved_candidate, resolved_root):
        return ("outside skills root", {})
    if candidate.is_symlink():
        return ("skill directory is a symlink", {})

    skill_md = candidate / "SKILL.md"
    # Learned skills follow session_prep.parse_skill_tier's current loader contract:
    # require a real SKILL.md here, but do not invent learned-only frontmatter fields.
    if not _is_regular_file_without_symlink(skill_md):
        return ("SKILL.md is missing, unreadable, or a symlink", {})

    size_bytes = _directory_size_bytes(candidate)
    if size_bytes is None:
        return ("contains symlink or unreadable file", {})
    if size_bytes > skill_max_bytes:
        return (
            "exceeds byte budget",
            {"size_bytes": size_bytes, "max_bytes": skill_max_bytes},
        )
    return None


def iter_learned_skill_dirs(group_folder: str) -> list[Path]:
    """Return learned skill directories selected from a group's learning profile."""
    paths = resolve_learning_paths(group_folder)
    if paths is None:
        return []

    skill_max_bytes = get_settings().learning.skill_max_bytes
    skill_dirs: list[Path] = []
    # Global Obsidian-backed skills are shared by every profile. Profile-scoped
    # skills are scanned after global ones so they can override an imported
    # shared skill with the same directory name during session sync.
    for skills_root in (paths.global_skills_root, paths.skills_root):
        skill_dirs.extend(_iter_skill_dirs_from_root(skills_root, skill_max_bytes))

    return skill_dirs


def find_learned_skill_dir(group_folder: str, skill_name: str) -> Path | None:
    """Return the validated learned skill with an exact public name."""
    return next(
        (path for path in iter_learned_skill_dirs(group_folder) if path.name == skill_name),
        None,
    )


def _iter_skill_dirs_from_root(skills_root: Path, skill_max_bytes: int) -> list[Path]:
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
            # Only direct files are materialized for agent skills. References
            # and templates remain available in the vault without invalidating
            # an otherwise usable skill.
            continue
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
