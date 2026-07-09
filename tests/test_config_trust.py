"""Tests for trust-model config parsing."""

import pytest
from pydantic import TypeAdapter, ValidationError

from pynchy.config.models import (
    BuiltinTool,
    McpTool,
    ToolConfig,
)
from pynchy.config.settings import validate_settings_mapping


def test_tool_trust_defaults_are_maximally_cautious():
    """Unpopulated tool trust config is maximally cautious."""
    cfg = TypeAdapter(ToolConfig).validate_python({"type": "builtin"})
    assert isinstance(cfg, BuiltinTool)
    assert cfg.public_source is True
    assert cfg.secret_data is True
    assert cfg.public_sink is True
    assert cfg.dangerous_writes is True


def test_tool_trust_all_false():
    """All-false tool trust config parses correctly."""
    cfg = TypeAdapter(ToolConfig).validate_python(
        {
            "type": "builtin",
            "public_source": False,
            "secret_data": False,
            "public_sink": False,
            "dangerous_writes": False,
        }
    )
    assert cfg.public_source is False
    assert cfg.dangerous_writes is False


def test_tool_trust_forbidden():
    """Forbidden string value parses correctly."""
    cfg = TypeAdapter(ToolConfig).validate_python(
        {
            "type": "builtin",
            "public_source": "forbidden",
            "public_sink": "forbidden",
            "dangerous_writes": "forbidden",
        }
    )
    assert cfg.public_source == "forbidden"


def test_tool_trust_invalid_value():
    """Invalid value raises ValidationError."""
    with pytest.raises(ValidationError):
        TypeAdapter(ToolConfig).validate_python({"type": "builtin", "public_source": "maybe"})


def test_mcp_tool_provider_config_parses_credentials_path():
    cfg = TypeAdapter(ToolConfig).validate_python(
        {
            "type": "mcp",
            "public_source": False,
            "secret_data": True,
            "public_sink": False,
            "dangerous_writes": False,
            "mcp": {"credentials_path": "/gdrive-server/credentials.json"},
        }
    )

    assert isinstance(cfg, McpTool)
    assert cfg.mcp.credentials_path == "/gdrive-server/credentials.json"


@pytest.mark.parametrize(
    ("mcp_config", "error"),
    [
        ({"runtime": "script", "port": 8080}, "Script MCP tools require 'command'"),
        ({"runtime": "script", "command": "uv"}, "Script MCP tools require 'port'"),
        ({"runtime": "docker", "port": 8080}, "Docker MCP tools require 'image'"),
        ({"runtime": "docker", "image": "mcp/example:latest"}, "Docker MCP tools require 'port'"),
        ({"runtime": "url"}, "URL MCP tools require 'url'"),
    ],
)
def test_mcp_tool_provider_config_rejects_incomplete_runtime_config(
    mcp_config: dict[str, object], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        TypeAdapter(ToolConfig).validate_python({"type": "mcp", "mcp": mcp_config})


def test_profile_selecting_unknown_tool_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown tool"):
        validate_settings_mapping(
            {
                "profiles": {"worker": {"tools": ["missing"]}},
                "workspaces": {"research": {"profiles": ["worker"]}},
                "tools": {},
            }
        )


def test_legacy_service_trust_toml_is_not_user_facing_config():
    """The old [services] trust shape is rejected at the Settings boundary."""
    with pytest.raises(ValidationError, match="Legacy config sections"):
        validate_settings_mapping({"services": {"browser": {"public_source": True}}})


def test_admin_profile_with_public_source_tool_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Admin workspace"):
        validate_settings_mapping(
            {
                "profiles": {"admin": {"is_admin": True, "tools": ["browser"]}},
                "workspaces": {"admin": {"profiles": ["admin"]}},
                "tools": {"browser": {"type": "builtin", "name": "browser", "public_source": True}},
            }
        )


def test_admin_profile_with_non_public_source_tool_passes() -> None:
    settings = validate_settings_mapping(
        {
            "profiles": {"admin": {"is_admin": True, "tools": ["shell"]}},
            "workspaces": {"admin": {"profiles": ["admin"]}},
            "tools": {"shell": {"type": "builtin", "name": "shell", "public_source": False}},
        }
    )

    assert settings.resolved_workspace_config("admin").tools == ["shell"]
