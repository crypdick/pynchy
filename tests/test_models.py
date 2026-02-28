"""Tests for config sub-models (SandboxProfileConfig, WorkspaceConfig fields)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config.models import (
    SandboxProfileConfig,
    WorkspaceConfig,
    WorkspaceSecurityTomlConfig,
)


class TestSandboxProfileConfigDefaults:
    """All fields default to None — the model expresses 'no opinion' by default."""

    def test_all_fields_default_to_none(self):
        cfg = SandboxProfileConfig()
        assert cfg.directives is None
        assert cfg.skills is None
        assert cfg.mcp_servers is None
        assert cfg.context_mode is None
        assert cfg.access is None
        assert cfg.mode is None
        assert cfg.trust is None
        assert cfg.trigger is None
        assert cfg.allowed_users is None
        assert cfg.idle_terminate is None
        assert cfg.git_policy is None
        assert cfg.security is None
        assert cfg.repo_access is None

    def test_default_instance_has_empty_fields_set(self):
        cfg = SandboxProfileConfig()
        assert cfg.model_fields_set == set()


class TestSandboxProfileConfigFieldsSet:
    """model_fields_set tracks only explicitly provided fields."""

    def test_single_field_tracked(self):
        cfg = SandboxProfileConfig(trust=True)
        assert cfg.model_fields_set == {"trust"}

    def test_none_explicit_is_tracked(self):
        """Explicitly passing None is still 'set' — distinguishable from default."""
        cfg = SandboxProfileConfig(access=None)
        assert "access" in cfg.model_fields_set

    def test_multiple_fields_tracked(self):
        cfg = SandboxProfileConfig(
            context_mode="isolated",
            skills=["code"],
            idle_terminate=False,
        )
        assert cfg.model_fields_set == {"context_mode", "skills", "idle_terminate"}


class TestSandboxProfileConfigListFields:
    """Union list fields accept values."""

    def test_directives_accepts_list(self):
        cfg = SandboxProfileConfig(directives=["safety", "code-style"])
        assert cfg.directives == ["safety", "code-style"]

    def test_skills_accepts_list(self):
        cfg = SandboxProfileConfig(skills=["core", "web"])
        assert cfg.skills == ["core", "web"]

    def test_mcp_servers_accepts_list(self):
        cfg = SandboxProfileConfig(mcp_servers=["github", "memory"])
        assert cfg.mcp_servers == ["github", "memory"]

    def test_allowed_users_accepts_list(self):
        cfg = SandboxProfileConfig(allowed_users=["owner"])
        assert cfg.allowed_users == ["owner"]


class TestSandboxProfileConfigScalarFields:
    """Override scalar fields accept valid values."""

    def test_context_mode_group(self):
        cfg = SandboxProfileConfig(context_mode="group")
        assert cfg.context_mode == "group"

    def test_context_mode_isolated(self):
        cfg = SandboxProfileConfig(context_mode="isolated")
        assert cfg.context_mode == "isolated"

    def test_access_literals(self):
        for val in ("read", "write", "readwrite"):
            cfg = SandboxProfileConfig(access=val)
            assert cfg.access == val

    def test_mode_literals(self):
        for val in ("agent", "chat"):
            cfg = SandboxProfileConfig(mode=val)
            assert cfg.mode == val

    def test_trust_bool(self):
        cfg = SandboxProfileConfig(trust=False)
        assert cfg.trust is False

    def test_trigger_literals(self):
        for val in ("mention", "always"):
            cfg = SandboxProfileConfig(trigger=val)
            assert cfg.trigger == val

    def test_idle_terminate_bool(self):
        cfg = SandboxProfileConfig(idle_terminate=True)
        assert cfg.idle_terminate is True

    def test_git_policy_literals(self):
        for val in ("merge-to-main", "pull-request"):
            cfg = SandboxProfileConfig(git_policy=val)
            assert cfg.git_policy == val

    def test_security_nested(self):
        sec = WorkspaceSecurityTomlConfig(contains_secrets=True)
        cfg = SandboxProfileConfig(security=sec)
        assert cfg.security is not None
        assert cfg.security.contains_secrets is True

    def test_repo_access_string(self):
        cfg = SandboxProfileConfig(repo_access="owner/repo")
        assert cfg.repo_access == "owner/repo"


class TestSandboxProfileConfigValidation:
    """extra='forbid' rejects unknown fields."""

    def test_rejects_unknown_field(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            SandboxProfileConfig(bogus="nope")

    def test_rejects_invalid_context_mode(self):
        with pytest.raises(ValidationError):
            SandboxProfileConfig(context_mode="invalid")

    def test_rejects_invalid_access(self):
        with pytest.raises(ValidationError):
            SandboxProfileConfig(access="admin")

    def test_rejects_invalid_git_policy(self):
        with pytest.raises(ValidationError):
            SandboxProfileConfig(git_policy="yolo")


class TestWorkspaceConfigNewFields:
    """New profile and directives fields on WorkspaceConfig."""

    def test_profile_defaults_to_none(self):
        cfg = WorkspaceConfig()
        assert cfg.profile is None

    def test_directives_defaults_to_none(self):
        cfg = WorkspaceConfig()
        assert cfg.directives is None

    def test_profile_accepts_string(self):
        cfg = WorkspaceConfig(profile="dev")
        assert cfg.profile == "dev"

    def test_directives_accepts_list(self):
        cfg = WorkspaceConfig(directives=["code-style", "safety"])
        assert cfg.directives == ["code-style", "safety"]

    def test_profile_in_fields_set(self):
        cfg = WorkspaceConfig(profile="prod")
        assert "profile" in cfg.model_fields_set

    def test_directives_in_fields_set(self):
        cfg = WorkspaceConfig(directives=["a"])
        assert "directives" in cfg.model_fields_set

    def test_both_absent_from_fields_set_by_default(self):
        cfg = WorkspaceConfig()
        assert "profile" not in cfg.model_fields_set
        assert "directives" not in cfg.model_fields_set
