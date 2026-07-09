"""Typed TOML I/O helpers for ``config.toml``.

Writers should mutate a TOML document here, render it, and validate the rendered
candidate through :class:`pynchy.config.settings.Settings` before writing. This
keeps comment-preserving edits from drifting away from the typed config schema.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomlkit

from pynchy.config.settings import Settings


def parse_settings_toml(text: str) -> Settings:
    """Parse TOML text through the typed Settings model."""
    data = tomllib.loads(text) if text.strip() else {}
    return Settings.model_validate(data)


def mutate_config_toml(path: Path, mutate: Callable[[Any], None]) -> Settings:
    """Apply a TOML mutation, validate the full candidate, then write it.

    The file is only written after the rendered candidate round-trips through
    ``Settings``. Validation errors are allowed to propagate so callers do not
    silently persist an unreadable config.
    """
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    mutate(doc)
    rendered = tomlkit.dumps(doc)
    settings = parse_settings_toml(rendered)
    path.write_text(rendered)
    return settings
