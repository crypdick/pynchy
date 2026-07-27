"""Discover skills in the canonical personalization repository."""

from __future__ import annotations

import stat
from pathlib import Path

from pynchy.config import get_settings
from pynchy.host.paths import PERSONALIZATION_RELATIVE_DIR, SKILLS_DIRNAME


def find_personalized_skill_dir(skill_name: str) -> Path | None:
    """Return one exact canonical skill directory without following symlinks."""
    if not skill_name or Path(skill_name).name != skill_name:
        return None
    skills_root = (
        get_settings().project_root / PERSONALIZATION_RELATIVE_DIR / SKILLS_DIRNAME
    ).resolve()
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
