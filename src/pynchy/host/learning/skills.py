"""Discover skills in the canonical personalization repository."""

from __future__ import annotations

import stat
from pathlib import Path

from pynchy.host.paths import PERSONALIZATION_RELATIVE_DIR, SKILLS_DIRNAME

_skills_root: Path | None = None


def configure_personalized_skills_root(project_root: Path) -> None:
    """Set the canonical personalization skill root during composition."""
    global _skills_root  # noqa: PLW0603, RUF100 - one host process owns one personalized skills root.
    _skills_root = (project_root / PERSONALIZATION_RELATIVE_DIR / SKILLS_DIRNAME).resolve()


def find_personalized_skill_dir(skill_name: str) -> Path | None:
    """Return one exact canonical skill directory without following symlinks."""
    if not skill_name or Path(skill_name).name != skill_name:
        return None
    if _skills_root is None:
        raise RuntimeError("personalized skills root has not been configured")
    skills_root = _skills_root
    candidate = skills_root / skill_name
    skill_file = candidate / "SKILL.md"
    try:
        if not stat.S_ISDIR(candidate.lstat().st_mode) or candidate.is_symlink():
            return None
        if not stat.S_ISREG(skill_file.lstat().st_mode) or skill_file.is_symlink():
            return None
    except OSError:
        return None
    return candidate.resolve()
