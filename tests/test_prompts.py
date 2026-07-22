"""Tests for convention-based prompt resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.config.prompts import read_prompts


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


def test_marketplace_health_prompt_routes_aggregate_reads_to_native_tool() -> None:
    project_root = Path(__file__).parents[1]

    result = read_prompts(["marketplace-health"], project_root)

    assert result is not None
    assert "mcp__pynchy__marketplace_health_snapshot" in result
    assert "Do not use Bash" in result
    assert "/Users/..." in result


def test_admin_prompt_routes_scheduled_work_reads_to_native_tool() -> None:
    project_root = Path(__file__).parents[1]

    result = read_prompts(["pynchy-admin-ops"], project_root)

    assert result is not None
    assert "mcp__pynchy__list_tasks" in result
    assert "Do not use Bash" in result
    assert "Answer immediately" in result
    assert "visibility limitation" in result
    assert "Do not discover, load, or read skills" in result
    assert "call `mcp__pynchy__list_tasks` once" in result


def test_base_prompt_requires_live_skill_catalog_discovery() -> None:
    project_root = Path(__file__).parents[1]

    result = read_prompts(["base"], project_root)

    assert result is not None
    assert "call `search_skills`" in result
    assert "before answering" in result
    assert "Finding a skill does not grant access" in result
    assert "Call `request_skill_access` only when the user asks" in result
