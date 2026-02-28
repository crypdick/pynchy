"""Tests for three-tier sandbox config merge logic."""

from __future__ import annotations

from pynchy.config.merge import ResolvedSandboxConfig, merge_sandbox_config
from pynchy.config.models import (
    SandboxProfileConfig,
    WorkspaceConfig,
    WorkspaceSecurityTomlConfig,
)


def _sandbox(**kwargs) -> WorkspaceConfig:
    """Shorthand to build a WorkspaceConfig with only specified fields."""
    return WorkspaceConfig(**kwargs)


def _profile(**kwargs) -> SandboxProfileConfig:
    """Shorthand to build a SandboxProfileConfig with only specified fields."""
    return SandboxProfileConfig(**kwargs)


# ---------------------------------------------------------------------------
# Union semantics
# ---------------------------------------------------------------------------


class TestUnionSemantics:
    """Union fields (directives, skills, mcp_servers) merge across tiers."""

    def test_union_from_all_three_tiers(self):
        universal = _profile(skills=["a"])
        profile = _profile(skills=["b"])
        sandbox = _sandbox(skills=["c"])
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.skills == ["a", "b", "c"]

    def test_union_deduplication(self):
        universal = _profile(skills=["a", "b"])
        profile = _profile(skills=["b", "c"])
        sandbox = _sandbox(skills=["c", "d"])
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.skills == ["a", "b", "c", "d"]

    def test_union_preserves_order(self):
        """First occurrence wins for ordering."""
        universal = _profile(directives=["z", "a"])
        profile = _profile(directives=["a", "m"])
        sandbox = _sandbox(directives=["m", "b"])
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.directives == ["z", "a", "m", "b"]

    def test_none_at_tier_contributes_nothing(self):
        universal = _profile(skills=["a"])
        profile = _profile()  # skills=None
        sandbox = _sandbox(skills=["b"])
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.skills == ["a", "b"]

    def test_all_none_yields_empty_list(self):
        result = merge_sandbox_config(_profile(), _profile(), _sandbox())
        assert result.skills == []
        assert result.directives == []
        assert result.mcp_servers == []

    def test_no_profile(self):
        universal = _profile(mcp_servers=["server-a"])
        sandbox = _sandbox(mcp_servers=["server-b"])
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.mcp_servers == ["server-a", "server-b"]

    def test_no_universal(self):
        profile = _profile(directives=["d1"])
        sandbox = _sandbox(directives=["d2"])
        result = merge_sandbox_config(None, profile, sandbox)
        assert result.directives == ["d1", "d2"]

    def test_no_universal_no_profile(self):
        sandbox = _sandbox(skills=["s1"])
        result = merge_sandbox_config(None, None, sandbox)
        assert result.skills == ["s1"]


# ---------------------------------------------------------------------------
# Override semantics
# ---------------------------------------------------------------------------


class TestOverrideSemantics:
    """Override fields: most-specific explicitly-set value wins."""

    def test_sandbox_overrides_profile(self):
        profile = _profile(mode="chat")
        sandbox = _sandbox(mode="agent")
        result = merge_sandbox_config(None, profile, sandbox)
        assert result.mode == "agent"

    def test_profile_overrides_universal(self):
        universal = _profile(trigger="always")
        profile = _profile(trigger="mention")
        result = merge_sandbox_config(universal, profile, _sandbox())
        assert result.trigger == "mention"

    def test_universal_provides_value(self):
        universal = _profile(access="read")
        result = merge_sandbox_config(universal, None, _sandbox())
        assert result.access == "read"

    def test_sandbox_overrides_all(self):
        universal = _profile(trust=False)
        profile = _profile(trust=False)
        sandbox = _sandbox(trust=True)
        result = merge_sandbox_config(universal, profile, sandbox)
        assert result.trust is True

    def test_no_tier_sets_field_uses_hardcoded_default(self):
        result = merge_sandbox_config(None, None, _sandbox())
        assert result.context_mode == "group"
        assert result.access == "readwrite"
        assert result.mode == "agent"
        assert result.trust is True
        assert result.trigger == "mention"
        assert result.allowed_users == ["owner"]
        assert result.idle_terminate is True
        assert result.git_policy == "merge-to-main"
        assert result.security is None
        assert result.repo_access is None

    def test_allowed_users_uses_override_not_union(self):
        """allowed_users is an override field despite being a list."""
        universal = _profile(allowed_users=["alice"])
        profile = _profile(allowed_users=["bob"])
        sandbox = _sandbox(allowed_users=["charlie"])
        result = merge_sandbox_config(universal, profile, sandbox)
        # Most-specific wins, no merging
        assert result.allowed_users == ["charlie"]

    def test_allowed_users_profile_overrides_universal(self):
        universal = _profile(allowed_users=["alice"])
        profile = _profile(allowed_users=["bob"])
        result = merge_sandbox_config(universal, profile, _sandbox())
        assert result.allowed_users == ["bob"]

    def test_allowed_users_falls_to_default(self):
        result = merge_sandbox_config(None, None, _sandbox())
        assert result.allowed_users == ["owner"]

    def test_idle_terminate_override(self):
        universal = _profile(idle_terminate=True)
        sandbox = _sandbox(idle_terminate=False)
        result = merge_sandbox_config(universal, None, sandbox)
        assert result.idle_terminate is False

    def test_git_policy_override(self):
        profile = _profile(git_policy="pull-request")
        result = merge_sandbox_config(None, profile, _sandbox())
        assert result.git_policy == "pull-request"

    def test_security_override(self):
        sec = WorkspaceSecurityTomlConfig(contains_secrets=True)
        profile = _profile(security=sec)
        result = merge_sandbox_config(None, profile, _sandbox())
        assert result.security is sec
        assert result.security.contains_secrets is True

    def test_repo_access_from_profile(self):
        profile = _profile(repo_access="org/repo")
        result = merge_sandbox_config(None, profile, _sandbox())
        assert result.repo_access == "org/repo"

    def test_repo_access_sandbox_overrides_profile(self):
        profile = _profile(repo_access="org/repo-a")
        sandbox = _sandbox(repo_access="org/repo-b")
        result = merge_sandbox_config(None, profile, sandbox)
        assert result.repo_access == "org/repo-b"

    def test_context_mode_from_sandbox(self):
        sandbox = _sandbox(context_mode="isolated")
        result = merge_sandbox_config(None, None, sandbox)
        assert result.context_mode == "isolated"


# ---------------------------------------------------------------------------
# Pass-through fields
# ---------------------------------------------------------------------------


class TestPassThrough:
    """Pass-through fields come from WorkspaceConfig, not tiers."""

    def test_chat_passes_through(self):
        sandbox = _sandbox(chat="connection.slack.main.chat.general")
        result = merge_sandbox_config(None, None, sandbox)
        assert result.chat == "connection.slack.main.chat.general"

    def test_chat_none(self):
        result = merge_sandbox_config(None, None, _sandbox())
        assert result.chat is None

    def test_is_admin_passes_through(self):
        sandbox = _sandbox(is_admin=True)
        result = merge_sandbox_config(None, None, sandbox)
        assert result.is_admin is True

    def test_is_admin_default(self):
        result = merge_sandbox_config(None, None, _sandbox())
        assert result.is_admin is False

    def test_schedule_passes_through(self):
        sandbox = _sandbox(schedule="0 9 * * *")
        result = merge_sandbox_config(None, None, sandbox)
        assert result.schedule == "0 9 * * *"

    def test_prompt_passes_through(self):
        sandbox = _sandbox(prompt="do the thing")
        result = merge_sandbox_config(None, None, sandbox)
        assert result.prompt == "do the thing"

    def test_name_passes_through(self):
        sandbox = _sandbox(name="my-sandbox")
        result = merge_sandbox_config(None, None, sandbox)
        assert result.name == "my-sandbox"

    def test_mcp_passes_through(self):
        sandbox = _sandbox(mcp={"server": {"key": "val"}})
        result = merge_sandbox_config(None, None, sandbox)
        assert result.mcp == {"server": {"key": "val"}}


# ---------------------------------------------------------------------------
# Integration / mixed scenarios
# ---------------------------------------------------------------------------


class TestMixedScenarios:
    """End-to-end scenarios combining union, override, and pass-through."""

    def test_full_three_tier_merge(self):
        universal = _profile(
            directives=["base"],
            skills=["core"],
            mode="chat",
            trust=False,
        )
        profile = _profile(
            directives=["profile-extra"],
            skills=["core", "web"],
            trigger="always",
            repo_access="org/repo",
        )
        sandbox = _sandbox(
            directives=["sandbox-special"],
            skills=["web", "data"],
            mode="agent",
            chat="connection.slack.main.chat.general",
            is_admin=True,
            name="test-sandbox",
        )
        result = merge_sandbox_config(universal, profile, sandbox)

        # Union fields
        assert result.directives == ["base", "profile-extra", "sandbox-special"]
        assert result.skills == ["core", "web", "data"]
        assert result.mcp_servers == []

        # Override fields
        assert result.mode == "agent"  # sandbox wins over universal's "chat"
        assert result.trust is False  # universal, nothing overrides
        assert result.trigger == "always"  # profile, nothing overrides
        assert result.repo_access == "org/repo"  # from profile
        assert result.context_mode == "group"  # hardcoded default
        assert result.allowed_users == ["owner"]  # hardcoded default

        # Pass-through
        assert result.chat == "connection.slack.main.chat.general"
        assert result.is_admin is True
        assert result.name == "test-sandbox"

    def test_result_is_frozen(self):
        result = merge_sandbox_config(None, None, _sandbox())
        assert isinstance(result, ResolvedSandboxConfig)
        # frozen dataclass raises on attribute assignment
        try:
            result.mode = "chat"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # noqa: TRY301
        except AttributeError:
            pass  # expected -- frozen
