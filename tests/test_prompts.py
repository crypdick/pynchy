"""Tests for layered prompt resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.config.api import (
    PersonalizationError,
    PersonalizationPaths,
    load_prompt_catalog,
)


def read_prompts(names: list[str], paths: PersonalizationPaths) -> str | None:
    if not names:
        return None
    return load_prompt_catalog(
        personalized_prompts=paths.personalized_prompts,
        default_prompts=paths.default_prompts,
    ).compose(names)


class TestReadPrompts:
    @pytest.fixture
    def paths(self, tmp_path: Path) -> PersonalizationPaths:
        defaults = tmp_path / "data" / "defaults" / "prompts"
        (defaults / "souls").mkdir(parents=True)
        (defaults / "executors").mkdir()
        (defaults / "souls" / "base.md").write_text("# Base\nShared instructions.")
        (defaults / "executors" / "admin-ops.md").write_text("# Admin Ops\nAdmin-only content.")
        return PersonalizationPaths.for_project(tmp_path)

    def test_reads_single_default_prompt(self, paths: PersonalizationPaths):
        result = read_prompts(["souls/base"], paths)
        assert result == "# Base\nShared instructions."

    def test_reads_multiple_prompts(self, paths: PersonalizationPaths):
        result = read_prompts(["souls/base", "executors/admin-ops"], paths)
        assert result is not None
        assert "# Base" in result
        assert "# Admin Ops" in result
        assert "---" in result

    def test_preserves_order(self, paths: PersonalizationPaths):
        result = read_prompts(["executors/admin-ops", "souls/base"], paths)
        assert result is not None
        assert result.index("Admin Ops") < result.index("Base")

    def test_duplicate_id_across_roots_fails(self, paths: PersonalizationPaths):
        (paths.personalized_prompts / "souls").mkdir(parents=True)
        (paths.personalized_prompts / "souls" / "base.md").write_text("# Private base")

        with pytest.raises(PersonalizationError, match="Duplicate prompt ID"):
            read_prompts(["souls/base"], paths)

    def test_empty_list_returns_none(self, paths: PersonalizationPaths):
        result = read_prompts([], paths)
        assert result is None

    def test_missing_file_fails(self, paths: PersonalizationPaths):
        with pytest.raises(PersonalizationError, match="Unknown prompt ID"):
            read_prompts(["souls/nonexistent"], paths)

    def test_blank_file_fails(self, paths: PersonalizationPaths):
        (paths.default_prompts / "souls" / "empty.md").write_text("")
        with pytest.raises(PersonalizationError, match="cannot be blank"):
            read_prompts(["souls/empty"], paths)


def test_base_prompt_requires_live_skill_catalog_discovery() -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(
        ["souls/default", "executors/default"],
        PersonalizationPaths.for_project(project_root),
    )

    assert result is not None
    assert "Use `search_skills` as the source of truth" in result
    normalized = " ".join(result.split())
    assert "Discovery does not grant access" in normalized
    assert "request access only when the user asks" in normalized


def test_base_prompt_distinguishes_blocking_questions_from_plain_text() -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(
        ["souls/default", "executors/default"],
        PersonalizationPaths.for_project(project_root),
    )

    assert result is not None
    assert "Use `ask_user` when" in result
    assert "an answer blocks the current task" in result
    assert "a plain-text question ends the turn" in result


def test_base_prompt_uses_intent_sensitive_agent_judgment() -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(
        ["souls/default", "executors/default"],
        PersonalizationPaths.for_project(project_root),
    )

    assert result is not None
    normalized = " ".join(result.split())
    assert "Proactively clear ordinary snags" in normalized
    assert "push without seeking renewed authorization" in normalized
    assert "Interpret authority in context" in normalized
    assert "exfiltrating private data or secrets" in normalized
    assert "Proceed with proportionate, recoverable fixes" in normalized


@pytest.mark.parametrize(
    ("prompt_id", "required_text"),
    [
        ("executors/planning", "call linear_submit_plan"),
        ("executors/delivery", "host verified approval"),
        ("executors/follow-up", "preserve useful logs before teardown"),
    ],
)
def test_linear_executor_contracts_are_public_prompts(
    prompt_id: str,
    required_text: str,
) -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(
        [prompt_id],
        PersonalizationPaths.for_project(project_root),
    )

    assert result is not None
    assert required_text in " ".join(result.split())


def test_delivery_prompt_bounds_worker_orchestration() -> None:
    project_root = Path(__file__).parents[1]
    result = read_prompts(
        ["executors/delivery"],
        PersonalizationPaths.for_project(project_root),
    )

    assert result is not None
    normalized = " ".join(result.split())
    assert "at most two bounded subagents" in normalized
    assert "one broad repository gate" in normalized
    assert "at most one independent review pass" in normalized
