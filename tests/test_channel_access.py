"""Tests for channel access mode resolution — the cascade logic in config_access.py.

Covers:
- resolve_channel_config: 4-level cascade (defaults → workspace → plugin → JID)
- resolve_allowed_users: group expansion, owner resolution, wildcard, cycle detection
- is_user_allowed: sender matching with platform-specific owner checks
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pynchy.config import Settings
from pynchy.config_access import (
    is_user_allowed,
    resolve_allowed_users,
    resolve_channel_config,
)
from pynchy.config_models import (
    ChannelOverrideConfig,
    OwnerConfig,
    WorkspaceConfig,
    WorkspaceDefaultsConfig,
)


def _settings_with(
    *,
    defaults: WorkspaceDefaultsConfig | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    owner: OwnerConfig | None = None,
    user_groups: dict[str, list[str]] | None = None,
) -> MagicMock:
    """Create a Settings mock for resolve_channel_config tests."""
    s = MagicMock(spec=Settings)
    s.workspace_defaults = defaults or WorkspaceDefaultsConfig()
    s.workspaces = workspaces or {}
    s.owner = owner or OwnerConfig()
    s.user_groups = user_groups or {}
    return s


# ---------------------------------------------------------------------------
# resolve_channel_config — cascade tests
# ---------------------------------------------------------------------------


class TestResolveChannelConfig:
    """Test the 4-level resolution cascade."""

    def test_defaults_when_no_workspace(self):
        """Unknown workspace → all defaults."""
        with patch("pynchy.config_access.get_settings", return_value=_settings_with()):
            result = resolve_channel_config("nonexistent")

        assert result.access == "readwrite"
        assert result.mode == "agent"
        assert result.trust is True
        assert result.trigger == "mention"
        assert result.allowed_users == ["owner"]

    def test_custom_defaults(self):
        """Custom workspace_defaults propagate."""
        defaults = WorkspaceDefaultsConfig(
            access="read",
            mode="chat",
            trust=False,
            trigger="always",
            allowed_users=["*"],
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(defaults=defaults),
        ):
            result = resolve_channel_config("nonexistent")

        assert result.access == "read"
        assert result.mode == "chat"
        assert result.trust is False
        assert result.trigger == "always"
        assert result.allowed_users == ["*"]

    def test_workspace_overrides_defaults(self):
        """Workspace-level fields override defaults."""
        ws = WorkspaceConfig(
            name="test",
            access="read",
            trigger="always",
            allowed_users=["*"],
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"lurker": ws}),
        ):
            result = resolve_channel_config("lurker")

        assert result.access == "read"
        assert result.trigger == "always"
        assert result.allowed_users == ["*"]
        # Unset fields inherit defaults
        assert result.mode == "agent"
        assert result.trust is True

    def test_workspace_partial_override(self):
        """Only set fields override — None fields inherit."""
        ws = WorkspaceConfig(name="test", mode="chat")
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"chat-ws": ws}),
        ):
            result = resolve_channel_config("chat-ws")

        assert result.mode == "chat"
        assert result.access == "readwrite"  # inherited
        assert result.trigger == "mention"  # inherited

    def test_channel_jid_overrides_workspace(self):
        """Per-channel JID config overrides workspace-level."""
        ws = WorkspaceConfig(
            name="test",
            trigger="mention",
            allowed_users=["owner"],
            channels={
                "slack:C04GENERAL": ChannelOverrideConfig(
                    trigger="always",
                    allowed_users=["*"],
                    mode="chat",
                ),
            },
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"hub": ws}),
        ):
            result = resolve_channel_config(
                "hub",
                channel_jid="slack:C04GENERAL",
            )

        assert result.trigger == "always"
        assert result.allowed_users == ["*"]
        assert result.mode == "chat"
        # Unset in channel override → inherited from workspace
        assert result.access == "readwrite"

    def test_channel_plugin_overrides_workspace(self):
        """Plugin-level channel config overrides workspace."""
        ws = WorkspaceConfig(
            name="test",
            trigger="mention",
            channels={
                "slack": ChannelOverrideConfig(access="read"),
            },
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"ws": ws}),
        ):
            result = resolve_channel_config(
                "ws",
                channel_plugin_name="slack",
            )

        assert result.access == "read"
        assert result.trigger == "mention"  # inherited from workspace

    def test_jid_overrides_plugin_level(self):
        """JID-specific config takes precedence over plugin-level."""
        ws = WorkspaceConfig(
            name="test",
            channels={
                "slack": ChannelOverrideConfig(access="read", mode="chat"),
                "slack:C04SPECIAL": ChannelOverrideConfig(
                    access="readwrite",
                    mode="agent",
                ),
            },
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"ws": ws}),
        ):
            result = resolve_channel_config(
                "ws",
                channel_jid="slack:C04SPECIAL",
                channel_plugin_name="slack",
            )

        # JID-level overrides plugin-level
        assert result.access == "readwrite"
        assert result.mode == "agent"

    def test_jid_partial_override_inherits_from_plugin(self):
        """JID override only sets some fields; rest come from plugin-level."""
        ws = WorkspaceConfig(
            name="test",
            channels={
                "slack": ChannelOverrideConfig(access="read", mode="chat"),
                "slack:C04SPECIAL": ChannelOverrideConfig(
                    access="readwrite",
                    # mode not set → inherits from plugin-level "chat"
                ),
            },
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"ws": ws}),
        ):
            result = resolve_channel_config(
                "ws",
                channel_jid="slack:C04SPECIAL",
                channel_plugin_name="slack",
            )

        assert result.access == "readwrite"  # from JID
        assert result.mode == "chat"  # from plugin-level

    def test_no_channel_override_uses_workspace(self):
        """Channel JID not in channels dict → workspace values used."""
        ws = WorkspaceConfig(
            name="test",
            access="write",
            channels={
                "slack:C04OTHER": ChannelOverrideConfig(access="read"),
            },
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"ws": ws}),
        ):
            result = resolve_channel_config(
                "ws",
                channel_jid="slack:C04NOMATCH",
            )

        assert result.access == "write"  # workspace level, not channel override


# ---------------------------------------------------------------------------
# resolve_allowed_users — group expansion
# ---------------------------------------------------------------------------


class TestResolveAllowedUsers:
    def test_wildcard_returns_none(self):
        """'*' in allowed_users → None (everyone allowed)."""
        result = resolve_allowed_users(["*"], {}, OwnerConfig())
        assert result is None

    def test_wildcard_with_other_entries(self):
        """'*' anywhere in the list → still None."""
        result = resolve_allowed_users(
            ["owner", "*", "slack:U123"],
            {},
            OwnerConfig(),
        )
        assert result is None

    def test_literal_user_ids(self):
        """Strings with ':' are literal user IDs."""
        result = resolve_allowed_users(
            ["slack:U04ABC", "whatsapp:1234@s.whatsapp.net"],
            {},
            OwnerConfig(),
        )
        assert result == {"slack:U04ABC", "whatsapp:1234@s.whatsapp.net"}

    def test_owner_resolution_slack(self):
        """'owner' resolves to slack ID when channel is slack."""
        owner = OwnerConfig(slack="U04MYID")
        result = resolve_allowed_users(
            ["owner"],
            {},
            owner,
            channel_plugin_name="slack",
        )
        assert result == {"slack:U04MYID"}

    def test_owner_resolution_whatsapp(self):
        """'owner' resolves to whatsapp:owner sentinel for WhatsApp."""
        result = resolve_allowed_users(
            ["owner"],
            {},
            OwnerConfig(),
            channel_plugin_name="whatsapp",
        )
        assert result == {"whatsapp:owner"}

    def test_group_expansion(self):
        """Group names expand to their members."""
        groups = {
            "engineering": ["slack:U04ALICE", "slack:U04BOB"],
        }
        result = resolve_allowed_users(
            ["engineering"],
            groups,
            OwnerConfig(),
        )
        assert result == {"slack:U04ALICE", "slack:U04BOB"}

    def test_nested_group_expansion(self):
        """Groups can reference other groups."""
        groups = {
            "engineering": ["slack:U04ALICE", "slack:U04BOB"],
            "leads": ["slack:U04CAROL"],
            "trusted": ["engineering", "leads"],
        }
        result = resolve_allowed_users(
            ["trusted"],
            groups,
            OwnerConfig(),
        )
        assert result == {
            "slack:U04ALICE",
            "slack:U04BOB",
            "slack:U04CAROL",
        }

    def test_cycle_detection(self):
        """Circular group references don't cause infinite recursion."""
        groups = {
            "a": ["b"],
            "b": ["a", "slack:U04X"],
        }
        result = resolve_allowed_users(["a"], groups, OwnerConfig())
        assert result == {"slack:U04X"}

    def test_mixed_entries(self):
        """Combination of owner, literal IDs, and group refs."""
        owner = OwnerConfig(slack="U04OWNER")
        groups = {
            "team": ["slack:U04A", "slack:U04B"],
        }
        result = resolve_allowed_users(
            ["owner", "team", "slack:U04FRIEND"],
            groups,
            owner,
            channel_plugin_name="slack",
        )
        assert result == {
            "slack:U04OWNER",
            "slack:U04A",
            "slack:U04B",
            "slack:U04FRIEND",
        }

    def test_unknown_group_ignored(self):
        """Referencing a non-existent group is silently ignored."""
        result = resolve_allowed_users(
            ["nonexistent_group"],
            {},
            OwnerConfig(),
        )
        assert result == set()

    def test_empty_list(self):
        """Empty allowed_users → empty set (nobody allowed)."""
        result = resolve_allowed_users([], {}, OwnerConfig())
        assert result == set()

    def test_owner_without_config_returns_empty(self):
        """'owner' with no config for the platform → no resolution."""
        result = resolve_allowed_users(
            ["owner"],
            {},
            OwnerConfig(),  # no slack configured
            channel_plugin_name="slack",
        )
        assert result == set()


# ---------------------------------------------------------------------------
# is_user_allowed — sender matching
# ---------------------------------------------------------------------------


class TestIsUserAllowed:
    def test_wildcard_allows_everyone(self):
        assert is_user_allowed("anyone", "slack", None) is True

    def test_qualified_sender_match(self):
        allowed = {"slack:U04ABC"}
        assert is_user_allowed("U04ABC", "slack", allowed) is True

    def test_qualified_sender_no_match(self):
        allowed = {"slack:U04ABC"}
        assert is_user_allowed("U04OTHER", "slack", allowed) is False

    def test_whatsapp_owner_via_is_from_me(self):
        allowed = {"whatsapp:owner"}
        assert (
            is_user_allowed(
                "someone",
                "whatsapp",
                allowed,
                is_from_me=True,
            )
            is True
        )

    def test_whatsapp_non_owner(self):
        allowed = {"whatsapp:owner"}
        assert (
            is_user_allowed(
                "someone",
                "whatsapp",
                allowed,
                is_from_me=False,
            )
            is False
        )

    def test_pre_qualified_sender(self):
        """Sender already contains platform prefix."""
        allowed = {"slack:U04ABC"}
        assert is_user_allowed("slack:U04ABC", None, allowed) is True

    def test_empty_allowed_set(self):
        assert is_user_allowed("anyone", "slack", set()) is False


# ---------------------------------------------------------------------------
# Composed behavior — use cases from the design doc
# ---------------------------------------------------------------------------


class TestComposedBehavior:
    """Test the use cases from the composed behavior matrix."""

    def test_personal_assistant(self):
        """1-on-1, no trigger needed."""
        ws = WorkspaceConfig(name="test", trigger="always")
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"assistant": ws}),
        ):
            result = resolve_channel_config("assistant")

        assert result.access == "readwrite"
        assert result.mode == "agent"
        assert result.trigger == "always"
        assert result.allowed_users == ["owner"]

    def test_lurk_and_summarize(self):
        """Read-only channel."""
        ws = WorkspaceConfig(name="test", access="read")
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"lurker": ws}),
        ):
            result = resolve_channel_config("lurker")

        assert result.access == "read"

    def test_announcement_bot(self):
        """Write-only channel."""
        ws = WorkspaceConfig(name="test", access="write")
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"standup": ws}),
        ):
            result = resolve_channel_config("standup")

        assert result.access == "write"

    def test_team_group_chat(self):
        """Team chat with tools disabled."""
        ws = WorkspaceConfig(
            name="test",
            mode="chat",
            trust=False,
            trigger="mention",
            allowed_users=["*"],
        )
        with patch(
            "pynchy.config_access.get_settings",
            return_value=_settings_with(workspaces={"team": ws}),
        ):
            result = resolve_channel_config("team")

        assert result.mode == "chat"
        assert result.trust is False
        assert result.trigger == "mention"
        assert result.allowed_users == ["*"]

    def test_hybrid_per_channel(self):
        """Different channels in the same workspace have different configs."""
        ws = WorkspaceConfig(
            name="test",
            trigger="mention",
            allowed_users=["owner"],
            channels={
                "slack:C04RESEARCH": ChannelOverrideConfig(access="read"),
                "slack:C04ANNOUNCE": ChannelOverrideConfig(access="write"),
                "slack:C04GENERAL": ChannelOverrideConfig(
                    trigger="mention",
                    allowed_users=["*"],
                    mode="chat",
                ),
            },
        )
        settings = _settings_with(workspaces={"hub": ws})
        with patch("pynchy.config_access.get_settings", return_value=settings):
            research = resolve_channel_config("hub", channel_jid="slack:C04RESEARCH")
            announce = resolve_channel_config("hub", channel_jid="slack:C04ANNOUNCE")
            general = resolve_channel_config("hub", channel_jid="slack:C04GENERAL")
            default = resolve_channel_config("hub")

        assert research.access == "read"
        assert announce.access == "write"
        assert general.mode == "chat"
        assert general.allowed_users == ["*"]
        assert default.access == "readwrite"  # workspace level
        assert default.trigger == "mention"


# ---------------------------------------------------------------------------
# Pydantic model validation
# ---------------------------------------------------------------------------


class TestChannelOverrideConfig:
    def test_all_none_is_valid(self):
        """A completely empty override is valid (inherits everything)."""
        cfg = ChannelOverrideConfig()
        assert cfg.access is None
        assert cfg.mode is None
        assert cfg.trust is None
        assert cfg.trigger is None
        assert cfg.allowed_users is None

    def test_partial_override(self):
        cfg = ChannelOverrideConfig(access="read", trust=False)
        assert cfg.access == "read"
        assert cfg.trust is False
        assert cfg.mode is None  # not set

    def test_invalid_access_rejected(self):
        with pytest.raises(ValueError, match="Input should be"):
            ChannelOverrideConfig(access="invalid")

    def test_invalid_trigger_rejected(self):
        with pytest.raises(ValueError, match="Input should be"):
            ChannelOverrideConfig(trigger="invalid")
