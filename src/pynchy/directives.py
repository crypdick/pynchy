"""Scoped system prompt directives — config-driven markdown injected into agent prompts.

Directives replace the old groups/global/CLAUDE.md overlay. Each directive has a
markdown file and a scope that determines which workspaces receive it.

Scope resolution:
- "all" → matches every workspace
- Contains "/" → repo slug, matches if workspace's repo_access equals it
- Otherwise → workspace folder name
- List → union of the above
- None (omitted) → never matches, logged as warning

Usage::

    from pynchy.directives import resolve_directives

    text = resolve_directives("admin-1", repo_access="crypdick/pynchy")
"""

from __future__ import annotations

from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger


def _scope_matches(
    scope_entry: str,
    workspace_folder: str,
    repo_access: str | None,
) -> bool:
    """Check if a single scope entry matches the given workspace."""
    if scope_entry == "all":
        return True
    if "/" in scope_entry:
        return repo_access == scope_entry
    return workspace_folder == scope_entry


def resolve_directives(workspace_folder: str, repo_access: str | None) -> str | None:
    """Resolve and concatenate matching directives for a workspace.

    Reads ``get_settings().directives``, checks each directive's scope against
    the workspace, reads matching files, and concatenates them in sorted key order.

    Returns None if no directives match (or all matched files are missing/empty).
    """
    s = get_settings()
    if not s.directives:
        return None

    project_root = s.project_root
    matched_parts: list[tuple[str, str]] = []  # (key, content) for sorting

    for key in sorted(s.directives):
        directive = s.directives[key]

        # Skip .EXAMPLE files
        if directive.file.endswith(".EXAMPLE"):
            continue

        # Check scope
        if directive.scope is None:
            logger.warning(
                "Directive has no scope, skipping",
                directive=key,
            )
            continue

        scopes = directive.scope if isinstance(directive.scope, list) else [directive.scope]
        if not any(_scope_matches(s, workspace_folder, repo_access) for s in scopes):
            continue

        # Read file
        file_path = project_root / directive.file
        if not file_path.exists():
            logger.warning(
                "Directive file not found, skipping",
                directive=key,
                path=str(file_path),
            )
            continue

        content = _read_directive_file(file_path)
        if content:
            matched_parts.append((key, content))

    if not matched_parts:
        return None

    return "\n\n---\n\n".join(content for _, content in matched_parts)


def _read_directive_file(path: Path) -> str | None:
    """Read a directive file, returning None on error or empty content."""
    try:
        text = path.read_text().strip()
        return text if text else None
    except OSError:
        logger.warning("Failed to read directive file", path=str(path))
        return None
