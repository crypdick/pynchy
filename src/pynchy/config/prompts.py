"""Layered prompt resolution for public defaults and personalization.

Prompt names map to files by convention. A personalized prompt takes precedence
over a same-named public default. Assignment remains the responsibility of profiles
and workspaces.

Usage::

    from pynchy.config.prompts import read_prompts

    paths = PersonalizationPaths.for_project(project_root)
    text = read_prompts(["base", "admin-ops"], paths)
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves annotations at runtime.

from pynchy.config.personalization import (  # noqa: TC001, RUF100 - beartype resolves annotations at runtime.
    PersonalizationPaths,
)
from pynchy.logger import logger


def read_prompts(names: list[str], paths: PersonalizationPaths) -> str | None:
    """Read and concatenate prompt files by name.

    A name resolves to ``data/personalization/prompts/<name>.md`` when present,
    otherwise to ``data/defaults/prompts/<name>.md``. Missing or empty files are
    warned about and skipped.

    Returns None if no prompts produce content.
    """
    if not names:
        return None

    parts: list[str] = []

    for name in names:
        file_path = _resolve_prompt_path(name, paths)
        if file_path is None:
            logger.warning(
                "Prompt file not found, skipping",
                prompt=name,
                personalization_path=str(paths.personalized_prompts / f"{name}.md"),
                default_path=str(paths.default_prompts / f"{name}.md"),
            )
            continue

        content = _read_file(file_path)
        if content:
            parts.append(content)

    if not parts:
        return None

    return "\n\n---\n\n".join(parts)


def _resolve_prompt_path(name: str, paths: PersonalizationPaths) -> Path | None:
    """Return the first concrete prompt file in priority order."""
    for directory in (paths.personalized_prompts, paths.default_prompts):
        candidate = directory / f"{name}.md"
        if candidate.is_file():
            return candidate
    return None


def _read_file(path: Path) -> str | None:
    """Read a file, returning None on error or empty content."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("Failed to read prompt file", path=str(path))
        return None
    else:
        return text if text else None
