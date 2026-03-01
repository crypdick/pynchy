"""Tests for convention-based directive resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.config.directives import read_directives


class TestReadDirectives:
    @pytest.fixture()
    def directives_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "directives"
        d.mkdir()
        (d / "base.md").write_text("# Base\nShared instructions.")
        (d / "admin-ops.md").write_text("# Admin Ops\nAdmin-only content.")
        (d / "repo-dev.md").write_text("# Repo Dev\nRepo-specific content.")
        return tmp_path

    def test_reads_single_directive(self, directives_dir: Path):
        result = read_directives(["base"], directives_dir)
        assert result == "# Base\nShared instructions."

    def test_reads_multiple_directives(self, directives_dir: Path):
        result = read_directives(["base", "admin-ops"], directives_dir)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result
        assert "---" in result

    def test_preserves_order(self, directives_dir: Path):
        result = read_directives(["admin-ops", "base"], directives_dir)
        assert result is not None
        assert result.index("Admin Ops") < result.index("Base")

    def test_empty_list_returns_none(self, directives_dir: Path):
        result = read_directives([], directives_dir)
        assert result is None

    def test_missing_file_warns_and_skips(self, directives_dir: Path):
        result = read_directives(["nonexistent"], directives_dir)
        assert result is None

    def test_missing_file_among_valid(self, directives_dir: Path):
        result = read_directives(["base", "nonexistent", "admin-ops"], directives_dir)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result

    def test_empty_file_skipped(self, directives_dir: Path):
        (directives_dir / "directives" / "empty.md").write_text("")
        result = read_directives(["empty"], directives_dir)
        assert result is None
