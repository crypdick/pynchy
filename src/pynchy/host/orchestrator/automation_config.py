"""Persistence helpers for config-backed automation definitions."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Mapping,
)
from pathlib import Path
from shutil import rmtree

import tomlkit

from pynchy.atomic_json import write_text_atomic


def _automation_path(project_root: Path, name: str) -> Path:
    if Path(name).name != name or not name or name.startswith("."):
        raise ValueError(f"Invalid automation name: {name!r}")
    return project_root / "data" / "personalization" / "automations" / name / "config.toml"


def add_job_to_toml(
    job_name: str,
    fields: Mapping[str, object],
    *,
    project_root: Path | None = None,
) -> None:
    """Write one file-backed automation definition."""
    automation_path = _automation_path(project_root or Path.cwd(), job_name)
    automation_path.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc.add("schema_version", tomlkit.item(1))
    job_table = tomlkit.table()
    for key, value in fields.items():
        job_table.add(key, value)
    doc.add("job", job_table)
    write_text_atomic(automation_path, tomlkit.dumps(doc))


def update_automation_toml(
    name: str,
    updates: dict[str, object],
    *,
    parse_and_dump: Callable[[dict[str, object]], Mapping[str, object]],
    project_root: Path | None = None,
) -> None:
    """Apply validated fields to one personalized automation definition."""
    automation_path = _automation_path(project_root or Path.cwd(), name)
    if not automation_path.is_file():
        raise ValueError(f"Automation not found: {name}")
    doc = tomlkit.parse(automation_path.read_text(encoding="utf-8"))
    job = doc.get("job")
    if not isinstance(job, dict):
        raise TypeError(f"Invalid automation definition: {name}")
    fields = parse_and_dump({**dict(job), **updates})
    replacement = tomlkit.table()
    for key, value in fields.items():
        replacement.add(key, value)
    doc["job"] = replacement
    write_text_atomic(automation_path, tomlkit.dumps(doc))


def delete_automation_toml(name: str, *, project_root: Path | None = None) -> None:
    """Remove one personalized automation directory after authorization."""
    automation_path = _automation_path(project_root or Path.cwd(), name)
    if not automation_path.is_file():
        raise ValueError(f"Automation not found: {name}")
    rmtree(automation_path.parent)
