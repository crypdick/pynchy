"""Executable tests for the first config schema cutover slice."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config import Settings
from pynchy.config.settings import validate_settings_mapping
from pynchy.config.toml_io import parse_settings_toml


def _settings_from_toml(source: str) -> Settings:
    return parse_settings_toml(source)


def _settings_from_dict(data: dict) -> Settings:
    return validate_settings_mapping(data)


def test_schema_validation_ignores_ambient_settings_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMMAND_CENTER__CONNECTION", "ambient")

    settings = _settings_from_dict(
        {
            "profiles": {"base": {}},
            "workspaces": {"engineering": {"profiles": ["base"]}},
        }
    )

    assert settings.command_center.connection is None


def test_new_schema_parses_minimal_config() -> None:
    settings = _settings_from_toml(
        """
        [agent]
        default_core = "codex"

        [profiles.base]
        prompts = ["base"]
        skills = ["python"]
        tools = ["shell", "linear"]
        repo = "owner/project"
        model = "chatgpt/gpt-5.3-codex"
        is_admin = false
        contains_secrets = false

        [workspaces.engineering]
        profiles = ["base"]

        [tools.shell]
        type = "builtin"
        name = "shell"

        [tools.linear]
        type = "linear"
        workspace = "PYN"

        [tools.docs]
        type = "mcp"

        [connections.synapse]
        type = "discord"
        bot_token_env = "DISCORD_BOT_TOKEN"

        [command_center]
        connection = "synapse"
        """
    )

    assert settings.agent.default_core == "codex"
    assert str(settings.repos.root) == "/Users/ricardo/src/PERSONAL"
    assert settings.profiles["base"].repo == ["owner/project"]
    assert settings.workspaces["engineering"].profiles == ["base"]
    assert settings.tools["shell"].type == "builtin"
    assert settings.tools["linear"].type == "linear"
    assert settings.tools["docs"].type == "mcp"
    assert settings.connections["synapse"].type == "discord"
    assert settings.command_center.connection == "synapse"


@pytest.mark.parametrize(
    "legacy_key",
    [
        "universal",
        "sandbox",
        "sandbox_universal",
        "sandbox_profiles",
        "services",
        "mcp",
        "mcp_servers",
        "mcp_groups",
        "mcp_presets",
        "mcp_server_instances",
        "connection",
        "owner",
        "caldav",
        "channels",
        "slack",
        "workspace_defaults",
        "directives",
        "cron_jobs",
    ],
)
def test_legacy_schema_keys_are_rejected(legacy_key: str) -> None:
    with pytest.raises(ValidationError, match="Legacy config sections"):
        _settings_from_dict({legacy_key: {}})


@pytest.mark.parametrize(
    "old_key",
    [
        "directives",
        "chat",
        "context_mode",
        "idle_terminate",
        "access",
        "mode",
        "trust",
        "trigger",
        "allowed_users",
        "fallback_model",
    ],
)
def test_workspace_can_only_select_profiles(old_key: str) -> None:
    with pytest.raises(ValidationError):
        _settings_from_dict(
            {
                "profiles": {"base": {"skills": ["python"]}},
                "workspaces": {"engineering": {"profiles": ["base"], old_key: "legacy"}},
            }
        )


def test_profiles_compose_in_order_with_union_fields_and_last_scalar_wins() -> None:
    settings = _settings_from_dict(
        {
            "profiles": {
                "base": {
                    "prompts": ["base", "shared"],
                    "skills": ["python"],
                    "tools": ["shell"],
                    "repo": "owner/base",
                    "model": "base-model",
                },
                "research": {
                    "includes": ["base"],
                    "prompts": ["shared", "research"],
                    "skills": ["web", "python"],
                    "tools": ["search", "shell"],
                    "repo": ["owner/base", "owner/research"],
                    "model": "research-model",
                    "contains_secrets": True,
                },
                "admin": {
                    "prompts": ["admin"],
                    "skills": ["ops"],
                    "tools": ["deploy"],
                    "repo": "owner/admin",
                    "is_admin": True,
                },
            },
            "workspaces": {"engineering": {"profiles": ["research", "admin"]}},
            "tools": {
                "shell": {"type": "builtin", "name": "shell", "public_source": False},
                "search": {"type": "builtin", "name": "search", "public_source": False},
                "deploy": {"type": "builtin", "name": "deploy", "public_source": False},
            },
        }
    )

    resolved = settings.resolved_workspace_config("engineering")

    assert resolved is not None
    assert resolved.prompts == ["base", "shared", "research", "admin"]
    assert resolved.skills == ["python", "web", "ops"]
    assert resolved.tools == ["shell", "search", "deploy"]
    assert resolved.repo == ["owner/base", "owner/research", "owner/admin"]
    assert resolved.model == "research-model"
    assert resolved.is_admin is True
    assert resolved.contains_secrets is True


def test_shared_included_profiles_apply_once_without_overwriting_later_scalars() -> None:
    settings = _settings_from_dict(
        {
            "profiles": {
                "base": {"model": "base-model", "tools": ["shell"]},
                "research": {"includes": ["base"], "model": "research-model"},
                "ops": {"includes": ["base"]},
            },
            "workspaces": {"engineering": {"profiles": ["research", "ops"]}},
            "tools": {"shell": {"type": "builtin", "name": "shell"}},
        }
    )

    resolved = settings.resolved_workspace_config("engineering")

    assert resolved is not None
    assert resolved.tools == ["shell"]
    assert resolved.model == "research-model"


def test_profile_tool_references_must_be_configured() -> None:
    with pytest.raises(ValidationError, match="unknown tool"):
        _settings_from_dict(
            {
                "profiles": {"base": {"tools": ["typo-tool"]}},
                "workspaces": {"engineering": {"profiles": ["base"]}},
            }
        )


def test_mcp_tool_rejects_loose_server_reference() -> None:
    with pytest.raises(ValidationError):
        _settings_from_dict({"tools": {"docs": {"type": "mcp", "server": "missing"}}})


def test_command_center_references_new_connection_names() -> None:
    settings = _settings_from_dict(
        {
            "connections": {"synapse": {"type": "discord", "bot_token_env": "DISCORD_BOT_TOKEN"}},
            "command_center": {"connection": "synapse"},
        }
    )

    assert settings.command_center.connection == "synapse"


def test_command_center_rejects_old_connection_refs() -> None:
    with pytest.raises(ValidationError):
        _settings_from_dict(
            {
                "connections": {
                    "synapse": {"type": "discord", "bot_token_env": "DISCORD_BOT_TOKEN"}
                },
                "command_center": {"connection": "connection.discord.synapse"},
            }
        )


def test_command_center_rejects_unknown_connection_names() -> None:
    with pytest.raises(ValidationError, match="unknown connection"):
        _settings_from_dict({"command_center": {"connection": "missing"}})


def test_profile_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError, match="profile cycle"):
        _settings_from_dict(
            {
                "profiles": {
                    "one": {"includes": ["two"]},
                    "two": {"includes": ["one"]},
                },
                "workspaces": {"engineering": {"profiles": ["one"]}},
            }
        )


def test_missing_profile_reference_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown profile"):
        _settings_from_dict(
            {
                "profiles": {"base": {"includes": ["missing"]}},
                "workspaces": {"engineering": {"profiles": ["base"]}},
            }
        )
