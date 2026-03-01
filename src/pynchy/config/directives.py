"""Convention-based directive resolution — reads directives/<name>.md files.

Directive names map to files by convention: "base" → directives/base.md.
No scope logic — assignment is handled by sandbox profiles.

Usage::

    from pynchy.config.directives import read_directives

    text = read_directives(["base", "admin-ops"], project_root)
"""

from __future__ import annotations

from pathlib import Path

from pynchy.logger import logger


def read_directives(names: list[str], project_root: Path) -> str | None:
    """Read and concatenate directive files by name.

    Maps each name to ``directives/<name>.md`` under *project_root*.
    Missing or empty files are warned about and skipped.

    Returns None if no directives produce content.
    """
    if not names:
        return None

    parts: list[str] = []

    for name in names:
        file_path = project_root / "directives" / f"{name}.md"
        if not file_path.exists():
            logger.warning(
                "Directive file not found, skipping",
                directive=name,
                path=str(file_path),
            )
            continue

        content = _read_file(file_path)
        if content:
            parts.append(content)

    if not parts:
        return None

    return "\n\n---\n\n".join(parts)


def _read_file(path: Path) -> str | None:
    """Read a file, returning None on error or empty content."""
    try:
        text = path.read_text().strip()
        return text if text else None
    except OSError:
        logger.warning("Failed to read directive file", path=str(path))
        return None
