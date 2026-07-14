"""Tests for config sub-models (ProfileConfig, WorkspaceConfig fields)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config.models import ProfileConfig, WorkspaceConfig


class TestProfileConfigDefaults:
    """Default profiles contribute no capabilities unless explicitly configured."""

    def test_default_fields(self):
        cfg = ProfileConfig()
        assert cfg.includes == []
        assert cfg.prompts == []
        assert cfg.skills == []
        assert cfg.tools == []
        assert cfg.repo == []
        assert cfg.model is None
        assert cfg.is_admin is False
        assert cfg.contains_secrets is False

    def test_default_instance_has_empty_fields_set(self):
        cfg = ProfileConfig()
        assert cfg.model_fields_set == set()


class TestProfileConfigFieldsSet:
    """model_fields_set tracks only explicitly provided fields."""

    def test_single_field_tracked(self):
        cfg = ProfileConfig(is_admin=True)
        assert cfg.model_fields_set == {"is_admin"}

    def test_none_explicit_is_tracked(self):
        cfg = ProfileConfig(model=None)
        assert "model" in cfg.model_fields_set

    def test_multiple_fields_tracked(self):
        cfg = ProfileConfig(
            prompts=["base"],
            skills=["code"],
            contains_secrets=True,
        )
        assert cfg.model_fields_set == {"prompts", "skills", "contains_secrets"}


class TestProfileConfigListFields:
    """Union list fields accept current schema values."""

    def test_prompts_accepts_list(self):
        cfg = ProfileConfig(prompts=["safety", "code-style"])
        assert cfg.prompts == ["safety", "code-style"]

    def test_includes_accepts_list(self):
        cfg = ProfileConfig(includes=["base", "dev"])
        assert cfg.includes == ["base", "dev"]

    def test_skills_accepts_list(self):
        cfg = ProfileConfig(skills=["core", "web"])
        assert cfg.skills == ["core", "web"]

    def test_tools_accepts_list(self):
        cfg = ProfileConfig(tools=["github", "memory"])
        assert cfg.tools == ["github", "memory"]

    def test_repo_accepts_list(self):
        cfg = ProfileConfig(repo=["owner/repo"])
        assert cfg.repo == ["owner/repo"]

    def test_repo_accepts_string(self):
        cfg = ProfileConfig(repo="owner/repo")
        assert cfg.repo == ["owner/repo"]


class TestProfileConfigScalarFields:
    """Scalar fields accept current schema values."""

    def test_model_string(self):
        cfg = ProfileConfig(model="chatgpt/gpt-5.3-codex-spark")
        assert cfg.model == "chatgpt/gpt-5.3-codex-spark"

    def test_boolean_flags(self):
        cfg = ProfileConfig(is_admin=True, contains_secrets=True)
        assert cfg.is_admin is True
        assert cfg.contains_secrets is True


class TestProfileConfigValidation:
    """extra='forbid' rejects unknown fields."""

    def test_rejects_unknown_field(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            ProfileConfig(bogus="nope")

    def test_rejects_empty_include_name(self):
        with pytest.raises(ValidationError):
            ProfileConfig(includes=[""])

    def test_rejects_empty_tool_name(self):
        with pytest.raises(ValidationError):
            ProfileConfig(tools=[""])

    def test_rejects_invalid_repo_slug(self):
        with pytest.raises(ValidationError):
            ProfileConfig(repo="owner")


class TestWorkspaceConfigFields:
    """Workspace config selects profiles and may override their model."""

    def test_profiles_defaults_to_empty_list(self):
        cfg = WorkspaceConfig()
        assert cfg.profiles == []

    def test_profiles_accepts_list(self):
        cfg = WorkspaceConfig(profiles=["dev", "admin"])
        assert cfg.profiles == ["dev", "admin"]

    def test_profiles_in_fields_set(self):
        cfg = WorkspaceConfig(profiles=["prod"])
        assert "profiles" in cfg.model_fields_set

    def test_profiles_absent_from_fields_set_by_default(self):
        cfg = WorkspaceConfig()
        assert "profiles" not in cfg.model_fields_set

    def test_model_defaults_to_none(self):
        cfg = WorkspaceConfig()
        assert cfg.model is None

    def test_model_accepts_string(self):
        cfg = WorkspaceConfig(model="chatgpt/gpt-5.3-codex-spark")
        assert cfg.model == "chatgpt/gpt-5.3-codex-spark"
        assert "model" in cfg.model_fields_set

    def test_rejects_empty_profile_name(self):
        with pytest.raises(ValidationError):
            WorkspaceConfig(profiles=[""])
