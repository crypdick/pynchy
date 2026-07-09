"""Convention-based prompt resolution -- reads prompts/<name>.md files.

Prompt names map to files by convention: "base" -> prompts/base.md.
No scope logic — assignment is handled by profiles and workspaces.

Usage::

    from pynchy.config.prompts import read_prompts

    text = read_prompts(["base", "admin-ops"], project_root)
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves annotations at runtime.

from pynchy.logger import logger


def read_prompts(names: list[str], project_root: Path) -> str | None:
    """Read and concatenate prompt files by name.

    Maps each name to ``prompts/<name>.md`` under *project_root*.
    Missing or empty files are warned about and skipped.

    Returns None if no prompts produce content.
    """
    if not names:
        return None

    parts: list[str] = []

    for name in names:
        file_path = project_root / "prompts" / f"{name}.md"
        if not file_path.exists():
            logger.warning(
                "Prompt file not found, skipping",
                prompt=name,
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
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("Failed to read prompt file", path=str(path))
        return None
    else:
        return text if text else None
