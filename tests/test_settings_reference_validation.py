"""Public settings-reference validation for configured routing boundaries."""

import pytest
from pydantic import ValidationError

from pynchy.config.api import validate_settings_mapping


def _slack_connection(*, chats: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "type": "slack",
        "bot_token_env": "SLACK_BOT_TOKEN",
        "app_token_env": "SLACK_APP_TOKEN",
        "chat": chats or {},
    }


def test_semantic_workspace_rejects_unknown_profile() -> None:
    with pytest.raises(
        ValidationError, match=r"workspaces\.reviews\.profiles references unknown profile"
    ):
        validate_settings_mapping(
            {
                "workspaces": {
                    "engineering": {
                        "threads": [
                            {
                                "name": "review",
                                "workspace": "reviews",
                                "profiles": ["missing"],
                            }
                        ]
                    }
                }
            }
        )


@pytest.mark.parametrize(
    ("connections", "chat", "error_type", "message"),
    [
        (
            {},
            "connection.slack.missing.chat.alerts",
            ValidationError,
            "references unknown connection",
        ),
        (
            {"primary": _slack_connection()},
            "connection.discord.primary.chat.guild.channels.general",
            TypeError,
            "requires a Discord connection",
        ),
        (
            {
                "primary": {
                    "type": "discord",
                    "bot_token_env": "DISCORD_BOT_TOKEN",
                    "chat": {"guild": {"channels": {"general": {"enabled": False}}}},
                }
            },
            "connection.discord.primary.chat.guild.channels.general",
            ValidationError,
            "disabled Discord channel",
        ),
    ],
)
def test_workspace_chat_requires_a_live_matching_connection(
    connections, chat, error_type, message
) -> None:
    with pytest.raises(error_type, match=message):
        validate_settings_mapping(
            {"connections": connections, "workspaces": {"team": {"chat": chat}}}
        )


@pytest.mark.parametrize(
    ("connections", "routes", "error_type", "message"),
    [
        (
            {},
            {"alerts": {"source": "connection.slack.missing.chat.alerts", "workspace": "team"}},
            ValidationError,
            "references an unknown connection",
        ),
        (
            {"primary": _slack_connection(chats={"alerts": {}})},
            {
                "alerts": {
                    "source": "connection.discord.primary.chat.alerts",
                    "workspace": "team",
                }
            },
            TypeError,
            "platform does not match",
        ),
        (
            {"primary": _slack_connection(chats={"other": {}})},
            {
                "alerts": {
                    "source": "connection.slack.primary.chat.alerts",
                    "workspace": "team",
                }
            },
            ValidationError,
            "references an unknown endpoint",
        ),
        (
            {"primary": _slack_connection(chats={"alerts": {}})},
            {
                "alerts": {
                    "source": "connection.slack.primary.chat.alerts",
                    "workspace": "missing",
                }
            },
            ValidationError,
            "references an unknown workspace",
        ),
        (
            {"primary": _slack_connection(chats={"alerts": {}})},
            {
                "first": {
                    "source": "connection.slack.primary.chat.alerts",
                    "workspace": "team",
                },
                "second": {
                    "source": "connection.slack.primary.chat.alerts",
                    "workspace": "team",
                },
            },
            ValidationError,
            "cannot map to multiple enabled routes",
        ),
    ],
)
def test_routes_require_existing_unique_sources_and_workspaces(
    connections, routes, error_type, message
) -> None:
    with pytest.raises(error_type, match=message):
        validate_settings_mapping(
            {"connections": connections, "routes": routes, "workspaces": {"team": {}}}
        )


@pytest.mark.parametrize(
    ("mcp", "message"),
    [
        ({"runtime": "stdio", "port": 8474}, "Stdio MCP tools require 'command'"),
        ({"runtime": "stdio", "command": "npx"}, "Stdio MCP tools require 'port'"),
        (
            {"runtime": "stdio", "command": "npx", "port": 8474, "transport": "sse"},
            "Stdio MCP tools require HTTP transport",
        ),
    ],
)
def test_stdio_mcp_requires_a_reachable_host_process(mcp, message) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_settings_mapping({"tools": {"local": {"type": "mcp", "mcp": mcp}}})


@pytest.mark.parametrize(
    ("tool", "message"),
    [
        (
            {"type": "builtin", "required_env": ["TOKEN", "TOKEN"]},
            "required_env names must be unique",
        ),
        (
            {"type": "builtin", "optional_env": ["TOKEN", "TOKEN"]},
            "optional_env names must be unique",
        ),
        (
            {"type": "builtin", "required_env": ["TOKEN"], "optional_env": ["TOKEN"]},
            "cannot be both required and optional",
        ),
        (
            {"type": "linear", "required_env": ["LINEAR_API_KEY", "SECOND_KEY"]},
            "exactly one required_env",
        ),
        (
            {"type": "linear", "optional_env": ["LINEAR_TEAM_KEY", "SECOND_TEAM"]},
            "at most one optional_env",
        ),
    ],
)
def test_tool_credentials_have_unambiguous_environment_contract(tool, message) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_settings_mapping({"tools": {"integration": tool}})
