"""Executable tests for the first config schema cutover slice."""

from __future__ import annotations

import tomllib

import pytest
from pydantic import ValidationError

from pynchy.config import Settings


def _settings_from_toml(source: str) -> Settings:
    return Settings(**tomllib.loads(source))


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
        server = "docs"

        [connections.discord]
        type = "discord"
        bot_token_env = "DISCORD_BOT_TOKEN"
        """
    )

    assert settings.agent.default_core == "codex"
    assert str(settings.repos.root) == "/Users/ricardo/src/PERSONAL"
    assert settings.profiles["base"].repo == ["owner/project"]
    assert settings.workspaces["engineering"].profiles == ["base"]
    assert settings.tools["shell"].type == "builtin"
    assert settings.tools["linear"].type == "linear"
    assert settings.tools["docs"].type == "mcp"
    assert settings.connections["discord"].type == "discord"


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
        Settings(**{legacy_key: {}})


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
        Settings(
            profiles={"base": {"skills": ["python"]}},
            workspaces={"engineering": {"profiles": ["base"], old_key: "legacy"}},
        )


def test_profiles_compose_in_order_with_union_fields_and_last_scalar_wins() -> None:
    settings = Settings(
        profiles={
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
        workspaces={"engineering": {"profiles": ["research", "admin"]}},
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


def test_profile_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError, match="profile cycle"):
        Settings(
            profiles={
                "one": {"includes": ["two"]},
                "two": {"includes": ["one"]},
            },
            workspaces={"engineering": {"profiles": ["one"]}},
        )


def test_missing_profile_reference_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown profile"):
        Settings(
            profiles={"base": {"includes": ["missing"]}},
            workspaces={"engineering": {"profiles": ["base"]}},
        )
