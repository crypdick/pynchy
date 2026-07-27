"""Load selected system prompts from concrete prompt directories."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves prompt directory annotations.

from pynchy.logger import logger


def read_prompts(
    names: list[str],
    *,
    personalized_prompts: Path,
    default_prompts: Path,
) -> str | None:
    """Read selected prompts, preferring personalized files."""
    parts: list[str] = []
    for name in names:
        file_path = next(
            (
                path
                for directory in (personalized_prompts, default_prompts)
                if (path := directory / f"{name}.md").is_file()
            ),
            None,
        )
        if file_path is None:
            logger.warning("Prompt file not found, skipping", prompt=name)
            continue
        try:
            content = file_path.read_text(encoding="utf-8").strip()
        except OSError:
            logger.warning("Failed to read prompt file", path=str(file_path))
            continue
        if content:
            parts.append(content)
    return "\n\n---\n\n".join(parts) or None
