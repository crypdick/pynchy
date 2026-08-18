"""Versioned automation document loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pynchy.config.errors import PersonalizationError
from pynchy.config.jobs import (  # noqa: TC001 - Pydantic resolves document annotations.
    JobConfig,
)


class AutomationDocument(BaseModel):
    """One versioned automation declaration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    job: JobConfig


def load_automations(directory: Path) -> dict[str, dict[str, Any]]:
    """Load direct files and directory-scoped automation documents."""
    if not directory.is_dir():
        return {}
    automations: dict[str, dict[str, Any]] = {}
    documents: list[tuple[str, Path, Path | None]] = [
        (path.stem, path, None) for path in sorted(directory.glob("*.toml"))
    ]
    documents.extend(
        (path.parent.name, path, path.parent.resolve())
        for path in sorted(directory.glob("*/config.toml"))
    )
    for name, path, automation_root in documents:
        if not name or name.startswith("."):
            raise PersonalizationError(f"Invalid automation name: {name!r}")
        if name in automations:
            raise PersonalizationError(f"Duplicate automation name: {name}")
        try:
            document = AutomationDocument.model_validate(
                tomllib.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise PersonalizationError(f"Invalid automation {path}: {exc}") from exc
        job = document.job
        if automation_root is not None:
            updates: dict[str, str] = {}
            if job.command is not None:
                updates["cwd"] = _resolve_automation_cwd(automation_root, job.cwd)
            if job.pre_run_command is not None:
                updates["pre_run_cwd"] = _resolve_automation_cwd(automation_root, job.pre_run_cwd)
            job = job.model_copy(update=updates)
        automations[name] = job.model_dump(exclude_none=True)
    return automations


def _resolve_automation_cwd(automation_root: Path, configured: str | None) -> str:
    if configured is None:
        return str(automation_root)
    path = Path(configured)
    return str(path if path.is_absolute() else (automation_root / path).resolve())
