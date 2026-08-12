"""Typed TOML I/O helpers for Pynchy settings.

Writers should mutate a TOML document here, render it, and validate the rendered
candidate through :class:`pynchy.config.settings.Settings` before writing. This
keeps comment-preserving edits from drifting away from the typed config schema.
"""

from __future__ import annotations

import tomllib
from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves annotations at runtime.
)
from pathlib import Path  # noqa: TC003 - beartype resolves annotations at runtime.
from typing import Any

import tomlkit

from pynchy.atomic_json import write_text_atomic
from pynchy.config.personalization import (
    SETTINGS_FILENAME,
    load_layered_settings_mapping,
)
from pynchy.config.settings import Settings, validate_settings_mapping


def parse_settings_toml(text: str) -> Settings:
    """Parse standalone TOML text without reading ambient settings sources."""
    data = tomllib.loads(text) if text.strip() else {}
    return validate_settings_mapping(data)


def mutate_config_toml(path: Path, mutate: Callable[[Any], None]) -> Settings:
    """Apply a TOML mutation, validate the full candidate, then write it.

    The file is only written after the rendered candidate round-trips through
    ``Settings``. Validation errors are allowed to propagate so callers do not
    silently persist an unreadable config.
    """
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    mutate(doc)
    rendered = tomlkit.dumps(doc)
    if path.name == SETTINGS_FILENAME and path.parent.name == "personalization":
        project_root = path.parent.parent.parent
        personal_data = tomllib.loads(rendered) if rendered.strip() else {}
        settings = validate_settings_mapping(
            load_layered_settings_mapping(
                project_root,
                personalization_root=path.parent,
                personalization_settings=personal_data,
            )
        )
    else:
        settings = parse_settings_toml(rendered)
    write_text_atomic(path, rendered)
    return settings
