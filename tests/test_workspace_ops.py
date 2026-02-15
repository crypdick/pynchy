"""Tests for workspace rename operations.

Tests the multi-step rename_workspace() function which coordinates
database updates, filesystem renames, and git worktree moves.
Errors here could corrupt workspace state or strand data.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.workspace_ops import RenameError, _rename_dir


class TestRenameDir:
    """Test the _rename_dir helper which renames directories with safety checks."""

    def test_renames_existing_directory(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        new = tmp_path / "new_dir"
        old.mkdir()
        (old / "file.txt").write_text("content")

        _rename_dir(old, new, "test")

        assert not old.exists()
        assert new.exists()
        assert (new / "file.txt").read_text() == "content"

    def test_skips_silently_when_source_does_not_exist(self, tmp_path: Path):
        old = tmp_path / "nonexistent"
        new = tmp_path / "new_dir"

        # Should not raise
        _rename_dir(old, new, "test")
        assert not new.exists()

    def test_raises_when_target_already_exists(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        new = tmp_path / "new_dir"
        old.mkdir()
        new.mkdir()

        with pytest.raises(RenameError, match="already exists"):
            _rename_dir(old, new, "test")

    def test_error_message_includes_label(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        new = tmp_path / "new_dir"
        old.mkdir()
        new.mkdir()

        with pytest.raises(RenameError, match="Target test directory"):
            _rename_dir(old, new, "test")

    def test_preserves_nested_directory_structure(self, tmp_path: Path):
        old = tmp_path / "old_dir"
        nested = old / "sub" / "deep"
        nested.mkdir(parents=True)
        (nested / "data.json").write_text("{}")

        new = tmp_path / "new_dir"
        _rename_dir(old, new, "test")

        assert (new / "sub" / "deep" / "data.json").read_text() == "{}"
