"""Atomic JSON persistence."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 - beartype resolves this runtime annotation.


def write_json_atomic(path: Path, data: object, *, indent: int | None = None) -> None:
    """Write JSON data through a temporary file and atomic rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=indent))
    tmp.rename(path)
