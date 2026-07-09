"""Tests for convention-based prompt resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.config.prompts import read_prompts

if TYPE_CHECKING:
    from pathlib import Path


class TestReadPrompts:
    @pytest.fixture
    def prompts_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "prompts"
        d.mkdir()
        (d / "base.md").write_text("# Base\nShared instructions.")
        (d / "admin-ops.md").write_text("# Admin Ops\nAdmin-only content.")
        (d / "repo-dev.md").write_text("# Repo Dev\nRepo-specific content.")
        return tmp_path

    def test_reads_single_prompt(self, prompts_dir: Path):
        result = read_prompts(["base"], prompts_dir)
        assert result == "# Base\nShared instructions."

    def test_reads_multiple_prompts(self, prompts_dir: Path):
        result = read_prompts(["base", "admin-ops"], prompts_dir)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result
        assert "---" in result

    def test_preserves_order(self, prompts_dir: Path):
        result = read_prompts(["admin-ops", "base"], prompts_dir)
        assert result is not None
        assert result.index("Admin Ops") < result.index("Base")

    def test_empty_list_returns_none(self, prompts_dir: Path):
        result = read_prompts([], prompts_dir)
        assert result is None

    def test_missing_file_warns_and_skips(self, prompts_dir: Path):
        result = read_prompts(["nonexistent"], prompts_dir)
        assert result is None

    def test_missing_file_among_valid(self, prompts_dir: Path):
        result = read_prompts(["base", "nonexistent", "admin-ops"], prompts_dir)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result

    def test_empty_file_skipped(self, prompts_dir: Path):
        (prompts_dir / "prompts" / "empty.md").write_text("")
        result = read_prompts(["empty"], prompts_dir)
        assert result is None
