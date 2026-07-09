"""Tests for channel access helpers that remain valid under profile composition."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config.access import (
    filter_allowed_messages,
    is_user_allowed,
    resolve_allowed_users,
    resolve_channel_config,
)
from pynchy.config.models import (
    ChannelOverrideConfig,
    ConnectionChatConfig,
    ConnectionsConfig,
    OwnerConfig,
    ProfileConfig,
    SlackConnectionConfig,
    WhatsAppConnectionConfig,
    WorkspaceConfig,
)
from pynchy.config.settings import Settings
from pynchy.types import NewMessage, WorkspaceProfile

SLACK_BOT_ENV = "BOT"
SLACK_APP_ENV = "APP"


def _message(sender: str, sender_name: str = "User") -> NewMessage:
    return NewMessage(
        id=f"msg-{sender}",
        chat_jid="slack:C123",
        sender=sender,
        sender_name=sender_name,
        content="hello",
        timestamp="2026-01-01T00:00:00Z",
    )


class TestResolveChannelConfig:
    """resolve_channel_config returns composable workspace profile resolution."""

    def test_defaults_when_no_workspace(self):
        with patch("pynchy.config.access.get_settings", return_value=make_settings()):
            result = resolve_channel_config("nonexistent")

        assert result.prompts == []
        assert result.skills == []
        assert result.tools == []
        assert result.repo == []
        assert result.model is None
        assert result.is_admin is False
        assert result.contains_secrets is False

    def test_selected_profiles_are_composed(self):
        settings = make_settings(
            profiles={
                "base": ProfileConfig(prompts=["base"], repo=["owner/base"]),
                "admin": ProfileConfig(
                    includes=["base"],
                    prompts=["admin"],
                    repo=["owner/admin"],
                    is_admin=True,
                    contains_secrets=True,
                ),
            },
            workspaces={"ops": WorkspaceConfig(profiles=["admin"])},
        )

        with patch("pynchy.config.access.get_settings", return_value=settings):
            result = resolve_channel_config("ops")

        assert result.prompts == ["base", "admin"]
        assert result.repo == ["owner/base", "owner/admin"]
        assert result.is_admin is True
        assert result.contains_secrets is True


class TestResolveAllowedUsers:
    def test_wildcard_returns_none(self):
        result = resolve_allowed_users(["*"], {}, OwnerConfig())
        assert result is None

    def test_wildcard_with_other_entries(self):
        result = resolve_allowed_users(
            ["owner", "*", "slack:U123"],
            {},
            OwnerConfig(),
        )
        assert result is None

    def test_literal_user_ids(self):
        result = resolve_allowed_users(
            ["slack:U04ABC", "whatsapp:1234@s.whatsapp.net"],
            {},
            OwnerConfig(),
        )
        assert result == {"slack:U04ABC", "whatsapp:1234@s.whatsapp.net"}

    def test_owner_resolution_slack(self):
        owner = OwnerConfig(slack="U04MYID")
        result = resolve_allowed_users(
            ["owner"],
            {},
            owner,
            channel_plugin_name="slack",
        )
        assert result == {"slack:U04MYID"}

    def test_owner_resolution_slack_name(self):
        owner = OwnerConfig(slack="ricardo")
        result = resolve_allowed_users(
            ["owner"],
            {},
            owner,
            channel_plugin_name="slack",
        )
        assert result == {"slack:ricardo"}

    def test_owner_resolution_whatsapp(self):
        result = resolve_allowed_users(
            ["owner"],
            {},
            OwnerConfig(),
            channel_plugin_name="whatsapp",
        )
        assert result == {"whatsapp:owner"}

    def test_group_expansion(self):
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
        groups = {
            "a": ["b"],
            "b": ["a", "slack:U04X"],
        }
        result = resolve_allowed_users(["a"], groups, OwnerConfig())
        assert result == {"slack:U04X"}

    def test_mixed_entries(self):
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
        result = resolve_allowed_users(
            ["nonexistent_group"],
            {},
            OwnerConfig(),
        )
        assert result == set()

    def test_empty_list(self):
        result = resolve_allowed_users([], {}, OwnerConfig())
        assert result == set()

    def test_owner_without_config_returns_empty(self):
        result = resolve_allowed_users(
            ["owner"],
            {},
            OwnerConfig(),
            channel_plugin_name="slack",
        )
        assert result == set()


class TestIsUserAllowed:
    def test_wildcard_allows_everyone(self):
        assert is_user_allowed("anyone", "slack", None) is True

    def test_qualified_sender_match(self):
        allowed = {"slack:U04ABC"}
        assert is_user_allowed("U04ABC", "slack", allowed) is True

    def test_qualified_sender_no_match(self):
        allowed = {"slack:U04ABC"}
        assert is_user_allowed("U04OTHER", "slack", allowed) is False

    def test_sender_name_match(self):
        allowed = {"slack:ricardo"}
        assert is_user_allowed("U04ABC", "slack", allowed, sender_name="Ricardo") is True

    def test_sender_name_no_match(self):
        allowed = {"slack:ricardo"}
        assert is_user_allowed("U04ABC", "slack", allowed, sender_name="Someone Else") is False

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
        allowed = {"slack:U04ABC"}
        assert is_user_allowed("slack:U04ABC", None, allowed) is True

    def test_empty_allowed_set(self):
        assert is_user_allowed("anyone", "slack", set()) is False


class TestFilterAllowedMessages:
    def test_filters_non_admin_messages_by_connection_security(self):
        settings = make_settings(
            connections={
                "synapse": SlackConnectionConfig(
                    bot_token_env=SLACK_BOT_ENV,
                    app_token_env=SLACK_APP_ENV,
                    security=ChannelOverrideConfig(allowed_users=["slack:U04ABC"]),
                )
            }
        )
        group = WorkspaceProfile(
            jid="slack:C123",
            name="Team",
            folder="team",
            trigger="@pynchy",
            added_at="2026-01-01T00:00:00Z",
        )
        messages = [_message("U04ABC"), _message("U04OTHER")]

        with patch("pynchy.config.access.get_settings", return_value=settings):
            filtered = filter_allowed_messages(messages, group, "synapse")

        assert [msg.sender for msg in filtered] == ["U04ABC"]

    def test_admin_bypasses_connection_security(self):
        settings = make_settings(
            connections={
                "synapse": SlackConnectionConfig(
                    bot_token_env=SLACK_BOT_ENV,
                    app_token_env=SLACK_APP_ENV,
                    security=ChannelOverrideConfig(allowed_users=["slack:U04ABC"]),
                )
            }
        )
        group = WorkspaceProfile(
            jid="slack:C123",
            name="Admin",
            folder="admin",
            trigger="@pynchy",
            added_at="2026-01-01T00:00:00Z",
            is_admin=True,
        )
        messages = [_message("U04ABC"), _message("U04OTHER")]

        with patch("pynchy.config.access.get_settings", return_value=settings):
            filtered = filter_allowed_messages(messages, group, "synapse")

        assert filtered == messages

    def test_missing_connection_policy_allows_messages(self):
        settings = make_settings()
        group = WorkspaceProfile(
            jid="slack:C123",
            name="Team",
            folder="team",
            trigger="@pynchy",
            added_at="2026-01-01T00:00:00Z",
        )
        messages = [_message("U04OTHER")]

        with patch("pynchy.config.access.get_settings", return_value=settings):
            filtered = filter_allowed_messages(messages, group, "synapse")

        assert filtered == messages

    def test_chat_security_overrides_connection_security(self):
        settings = make_settings(
            connections={
                "synapse": SlackConnectionConfig(
                    bot_token_env=SLACK_BOT_ENV,
                    app_token_env=SLACK_APP_ENV,
                    security=ChannelOverrideConfig(allowed_users=["slack:U04CONNECTION"]),
                    chat={
                        "C123": ConnectionChatConfig(
                            security=ChannelOverrideConfig(allowed_users=["slack:U04CHAT"])
                        )
                    },
                )
            }
        )
        group = WorkspaceProfile(
            jid="slack:C123",
            name="Team",
            folder="team",
            trigger="@pynchy",
            added_at="2026-01-01T00:00:00Z",
        )
        messages = [_message("U04CONNECTION"), _message("U04CHAT")]

        with patch("pynchy.config.access.get_settings", return_value=settings):
            filtered = filter_allowed_messages(messages, group, "synapse")

        assert [msg.sender for msg in filtered] == ["U04CHAT"]


class TestChannelOverrideConfig:
    def test_all_none_is_valid(self):
        cfg = ChannelOverrideConfig()
        assert cfg.allowed_users is None

    def test_allowed_users_override(self):
        cfg = ChannelOverrideConfig(allowed_users=["slack:U04ABC"])
        assert cfg.allowed_users == ["slack:U04ABC"]

    def test_deleted_access_rejected(self):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            ChannelOverrideConfig(access="read")

    def test_deleted_trigger_rejected(self):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            ChannelOverrideConfig(trigger="always")


class TestConnectionsConfigGetConnection:
    def test_slack_lookup(self):
        slack_bot_env = "SLACK_BOT_ENV"
        slack_app_env = "SLACK_APP_ENV"
        connections = ConnectionsConfig(
            slack={
                "main": SlackConnectionConfig(
                    bot_token_env=slack_bot_env,
                    app_token_env=slack_app_env,
                )
            }
        )
        result = connections.get_connection("slack", "main")
        assert isinstance(result, SlackConnectionConfig)
        assert result.bot_token_env == slack_bot_env

    def test_whatsapp_lookup(self):
        connections = ConnectionsConfig(
            whatsapp={"phone1": WhatsAppConnectionConfig(auth_db_path="/tmp/wa.db")}
        )
        result = connections.get_connection("whatsapp", "phone1")
        assert isinstance(result, WhatsAppConnectionConfig)
        assert result.auth_db_path == "/tmp/wa.db"

    def test_unknown_platform_returns_none(self):
        connections = ConnectionsConfig()
        assert connections.get_connection("telegram", "main") is None

    def test_unknown_name_returns_none(self):
        slack_bot_env = "SLACK_BOT_ENV"
        slack_app_env = "SLACK_APP_ENV"
        connections = ConnectionsConfig(
            slack={
                "main": SlackConnectionConfig(
                    bot_token_env=slack_bot_env,
                    app_token_env=slack_app_env,
                )
            }
        )
        assert connections.get_connection("slack", "other") is None

    def test_connection_chat_config_accepts_security_override(self):
        cfg = ConnectionChatConfig(security=ChannelOverrideConfig(allowed_users=["slack:U04ABC"]))
        assert cfg.security is not None
        assert cfg.security.allowed_users == ["slack:U04ABC"]

    def test_slack_owner_alias_is_rejected(self):
        with pytest.raises(ValueError, match="owner aliases are only supported for WhatsApp"):
            Settings(
                connections={
                    "synapse": SlackConnectionConfig(
                        bot_token_env=SLACK_BOT_ENV,
                        app_token_env=SLACK_APP_ENV,
                        security=ChannelOverrideConfig(allowed_users=["owner"]),
                    )
                }
            )

    def test_whatsapp_owner_alias_does_not_need_owner_config(self):
        settings = Settings(
            connections={
                "phone": WhatsAppConnectionConfig(
                    security=ChannelOverrideConfig(allowed_users=["owner"])
                )
            }
        )

        assert settings.connections["phone"].security.allowed_users == ["owner"]
