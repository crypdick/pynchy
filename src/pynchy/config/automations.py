"""Versioned automation document loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from pynchy.config.errors import PersonalizationError
from pynchy.config.jobs import (
    JobConfig,
)


class AutomationDocument(BaseModel):
    """One versioned automation declaration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    job: JobConfig


def load_automations(directory: Path) -> dict[str, dict[str, Any]]:
    """Load directory-scoped automation documents."""
    if not directory.is_dir():
        return {}
    flat_files = sorted(directory.glob("*.toml"))
    if flat_files:
        names = ", ".join(path.name for path in flat_files)
        raise PersonalizationError(
            f"Automation files must use automations/<name>/config.toml; found flat files: {names}"
        )
    automations: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*/config.toml")):
        name = path.parent.name
        if not name or name.startswith("."):
            raise PersonalizationError(f"Invalid automation name: {name!r}")
        try:
            document = AutomationDocument.model_validate(
                tomllib.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise PersonalizationError(f"Invalid automation {path}: {exc}") from exc
        job = document.job
        automation_root = path.parent.resolve()
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
