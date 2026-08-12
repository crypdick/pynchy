"""Atomic JSON persistence behavior."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from pynchy.atomic_json import write_json_atomic

if TYPE_CHECKING:
    from pathlib import Path


def test_atomic_json_write_creates_parent_and_replaces_existing_value(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"

    write_json_atomic(path, {"version": 1}, indent=2)
    write_json_atomic(path, {"version": 2})

    assert json.loads(path.read_text()) == {"version": 2}
    assert not path.with_suffix(".json.tmp").exists()


def test_atomic_json_write_does_not_follow_predictable_temp_symlink(tmp_path: Path) -> None:
    path = tmp_path / "shared" / "response.json"
    outside = tmp_path / "outside.json"
    path.parent.mkdir()
    outside.write_text("untouched")
    path.with_suffix(".json.tmp").symlink_to(outside)

    write_json_atomic(path, {"result": "safe"})

    assert outside.read_text() == "untouched"
    assert json.loads(path.read_text()) == {"result": "safe"}


def test_atomic_json_write_removes_temp_after_replace_failure(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    with (
        patch("pynchy.atomic_json.os.replace", side_effect=OSError("replace failed")),
        pytest.raises(OSError, match="replace failed"),
    ):
        write_json_atomic(path, {"result": "not-published"})

    assert not list(tmp_path.iterdir())


def test_atomic_json_write_preserves_existing_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version": 1}')
    path.chmod(0o600)

    write_json_atomic(path, {"version": 2})

    assert path.stat().st_mode & 0o777 == 0o600
