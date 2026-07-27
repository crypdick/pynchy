"""Tests for layered prompt resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.config.personalization import PersonalizationPaths
from pynchy.host.orchestrator.prompt_loading import read_prompts as _read_prompts


def read_prompts(names: list[str], paths: PersonalizationPaths) -> str | None:
    return _read_prompts(
        names,
        personalized_prompts=paths.personalized_prompts,
        default_prompts=paths.default_prompts,
    )


class TestReadPrompts:
    @pytest.fixture
    def paths(self, tmp_path: Path) -> PersonalizationPaths:
        defaults = tmp_path / "data" / "defaults" / "prompts"
        defaults.mkdir(parents=True)
        (defaults / "base.md").write_text("# Base\nShared instructions.")
        (defaults / "admin-ops.md").write_text("# Admin Ops\nAdmin-only content.")
        (defaults / "repo-dev.md").write_text("# Repo Dev\nRepo-specific content.")
        return PersonalizationPaths.for_project(tmp_path)

    def test_reads_single_default_prompt(self, paths: PersonalizationPaths):
        result = read_prompts(["base"], paths)
        assert result == "# Base\nShared instructions."

    def test_reads_multiple_prompts(self, paths: PersonalizationPaths):
        result = read_prompts(["base", "admin-ops"], paths)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result
        assert "---" in result

    def test_preserves_order(self, paths: PersonalizationPaths):
        result = read_prompts(["admin-ops", "base"], paths)
        assert result is not None
        assert result.index("Admin Ops") < result.index("Base")

    def test_personalized_prompt_replaces_default(self, paths: PersonalizationPaths):
        paths.personalized_prompts.mkdir(parents=True)
        (paths.personalized_prompts / "base.md").write_text("# Private base")

        assert read_prompts(["base"], paths) == "# Private base"

    def test_empty_list_returns_none(self, paths: PersonalizationPaths):
        result = read_prompts([], paths)
        assert result is None

    def test_missing_file_warns_and_skips(self, paths: PersonalizationPaths):
        result = read_prompts(["nonexistent"], paths)
        assert result is None

    def test_missing_file_among_valid(self, paths: PersonalizationPaths):
        result = read_prompts(["base", "nonexistent", "admin-ops"], paths)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result

    def test_empty_personalized_file_replaces_default(self, paths: PersonalizationPaths):
        paths.personalized_prompts.mkdir(parents=True)
        (paths.default_prompts / "empty.md").write_text("public prompt")
        (paths.personalized_prompts / "empty.md").write_text("")
        result = read_prompts(["empty"], paths)
        assert result is None


def test_base_prompt_requires_live_skill_catalog_discovery() -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(["base"], PersonalizationPaths.for_project(project_root))

    assert result is not None
    assert "Use `search_skills` as the source of truth" in result
    normalized = " ".join(result.split())
    assert "Discovery does not grant access" in normalized
    assert "request access only when the user asks" in normalized


def test_base_prompt_distinguishes_blocking_questions_from_plain_text() -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(["base"], PersonalizationPaths.for_project(project_root))

    assert result is not None
    assert "Use `ask_user` when" in result
    assert "an answer blocks the current task" in result
    assert "a plain-text question ends the turn" in result


def test_base_prompt_uses_intent_sensitive_agent_judgment() -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(["base"], PersonalizationPaths.for_project(project_root))

    assert result is not None
    normalized = " ".join(result.split())
    assert "Proactively clear ordinary snags" in normalized
    assert "Interpret authority in context" in normalized
    assert "exfiltrating private data or secrets" in normalized
    assert "Proceed with proportionate, recoverable fixes" in normalized
