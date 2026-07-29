"""Atomic JSON persistence behavior."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pynchy.atomic_json import write_json_atomic

if TYPE_CHECKING:
    from pathlib import Path


def test_atomic_json_write_creates_parent_and_replaces_existing_value(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"

    write_json_atomic(path, {"version": 1}, indent=2)
    write_json_atomic(path, {"version": 2})

    assert json.loads(path.read_text()) == {"version": 2}
    assert not path.with_suffix(".json.tmp").exists()
