"""Tests for layered prompt resolution and required prompt behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.config.api import (
    PersonalizationError,
    PersonalizationPaths,
    PipelineConfig,
    PipelineStageConfig,
    PromptConfig,
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
        (defaults / "webhooks").mkdir()
        (defaults / "souls" / "base.md").write_text("# Base\nShared instructions.")
        (defaults / "executors" / "admin-ops.md").write_text("# Admin Ops\nAdmin-only content.")
        (defaults / "webhooks" / "linear.md").write_text("# Linear\nWebhook content.")
        return PersonalizationPaths.for_project(tmp_path)

    def test_reads_single_default_prompt(self, paths: PersonalizationPaths):
        result = read_prompts(["souls/base"], paths)
        assert result == "# Base\nShared instructions."

    def test_reads_webhook_prompt(self, paths: PersonalizationPaths):
        assert read_prompts(["webhooks/linear"], paths) == "# Linear\nWebhook content."

    def test_reads_nested_webhook_prompt(self, paths: PersonalizationPaths):
        nested = paths.default_prompts / "webhooks" / "linear"
        nested.mkdir()
        (nested / "comment.md").write_text("# Comment\nNested webhook content.")

        assert read_prompts(["webhooks/linear/comment"], paths) == (
            "# Comment\nNested webhook content."
        )

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

    def test_invalid_file_layout_fails(self, paths: PersonalizationPaths):
        nested = paths.default_prompts / "souls" / "nested"
        nested.mkdir()
        (nested / "prompt.md").write_text("content")

        with pytest.raises(PersonalizationError, match="must be flat"):
            load_prompt_catalog(
                default_prompts=paths.default_prompts,
                personalized_prompts=paths.personalized_prompts,
            )

    def test_invalid_prompt_scope_fails(self, paths: PersonalizationPaths):
        invalid_scope = paths.default_prompts / "unknown"
        invalid_scope.mkdir()
        (invalid_scope / "prompt.md").write_text("content")

        with pytest.raises(PersonalizationError, match="webhooks/"):
            load_prompt_catalog(
                default_prompts=paths.default_prompts,
                personalized_prompts=paths.personalized_prompts,
            )

    def test_symlinked_prompt_fails(self, paths: PersonalizationPaths):
        target = paths.default_prompts / "souls" / "target.md"
        target.write_text("content")
        (paths.default_prompts / "souls" / "link.md").symlink_to(target)

        with pytest.raises(PersonalizationError, match="must be flat"):
            load_prompt_catalog(
                default_prompts=paths.default_prompts,
                personalized_prompts=paths.personalized_prompts,
            )

    def test_invalid_prompt_id_fails(self, paths: PersonalizationPaths):
        (paths.default_prompts / "souls" / "Bad.md").write_text("content")

        with pytest.raises(PersonalizationError, match="Invalid prompt ID"):
            load_prompt_catalog(
                default_prompts=paths.default_prompts,
                personalized_prompts=paths.personalized_prompts,
            )

    def test_prompt_read_failure_is_reported(
        self, paths: PersonalizationPaths, monkeypatch: pytest.MonkeyPatch
    ):
        prompt = paths.default_prompts / "souls" / "unreadable.md"
        prompt.write_text("content")
        monkeypatch.setattr(
            Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom"))
        )

        with pytest.raises(PersonalizationError, match="Could not read prompt"):
            load_prompt_catalog(
                default_prompts=paths.default_prompts,
                personalized_prompts=paths.personalized_prompts,
            )


def test_prompt_scope_and_pipeline_validation() -> None:
    with pytest.raises(ValueError, match="executors/ scope"):
        PipelineStageConfig(name="interactive", executor="souls/default")
    with pytest.raises(ValueError, match="default pipeline cannot be empty"):
        PromptConfig(default_pipeline="  ")
    with pytest.raises(ValueError, match="unique ignoring case"):
        PipelineConfig(
            stages=[
                PipelineStageConfig(name="interactive", executor="executors/default"),
                PipelineStageConfig(name="interactive", executor="executors/default"),
            ]
        )


@pytest.mark.parametrize(
    ("values", "scope"),
    [
        ({"default_executor": "souls/default"}, "executors/ scope"),
        ({"cop_inbound": "executors/default"}, "reviewers/ scope"),
    ],
)
def test_prompt_config_rejects_cross_scope_roles(values, scope) -> None:
    with pytest.raises(ValueError, match=scope):
        PromptConfig(**values)
