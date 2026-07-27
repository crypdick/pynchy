"""Tests for trust-model config parsing."""

import pytest
from pydantic import TypeAdapter, ValidationError

from pynchy.config.models import (
    BuiltinTool,
    LinearTool,
    MatrixConnectionConfig,
    McpTool,
    ToolConfig,
    WorkspaceTool,
)
from pynchy.config.settings import validate_settings_mapping
from pynchy.host.container_manager.mcp.resolution import merged_mcp_servers


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


def test_workspace_tool_owns_skills_and_environment_access() -> None:
    cfg = TypeAdapter(ToolConfig).validate_python(
        {
            "type": "workspace",
            "skills": ["github-auth"],
            "required_env": ["GITHUB_TOKEN"],
        }
    )

    assert isinstance(cfg, WorkspaceTool)
    assert cfg.skills == ["github-auth"]
    assert cfg.required_env == ["GITHUB_TOKEN"]
    assert cfg.expose_env_to_workspace is True


def test_workspace_tool_rejects_disabling_inherent_workspace_exposure() -> None:
    with pytest.raises(ValidationError, match="expose_env_to_workspace"):
        TypeAdapter(ToolConfig).validate_python(
            {"type": "workspace", "expose_env_to_workspace": False}
        )


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


def test_builtin_integration_schemas_parse_at_the_config_boundary() -> None:
    linear = TypeAdapter(ToolConfig).validate_python({"type": "linear"})
    matrix = validate_settings_mapping(
        {"connections": {"matrix": {"type": "matrix", "expected_user_id": "@owner:test"}}}
    ).connections["matrix"]

    assert isinstance(linear, LinearTool)
    assert isinstance(matrix, MatrixConnectionConfig)


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
            "mcp": {
                "runtime": "docker",
                "image": "mcp/gdrive:latest",
                "port": 8080,
                "credentials_path": "/gdrive-server/credentials.json",
            },
        }
    )

    assert isinstance(cfg, McpTool)
    assert cfg.mcp.credentials_path == "/gdrive-server/credentials.json"


def test_mcp_tool_rejects_missing_provider_config() -> None:
    with pytest.raises(ValidationError, match="mcp"):
        TypeAdapter(ToolConfig).validate_python({"type": "mcp"})


def test_mcp_tool_rejects_removed_env_forward() -> None:
    with pytest.raises(ValidationError, match="env_forward"):
        TypeAdapter(ToolConfig).validate_python(
            {
                "type": "mcp",
                "mcp": {
                    "runtime": "script",
                    "command": "uv",
                    "port": 8475,
                    "env_forward": {"TOKEN": "HOST_TOKEN"},
                },
            }
        )


def test_mcp_tool_provider_config_rejects_implicit_partial_docker_config() -> None:
    with pytest.raises(ValidationError, match="Docker MCP tools require 'port'"):
        TypeAdapter(ToolConfig).validate_python(
            {"type": "mcp", "mcp": {"image": "mcp/example:latest"}}
        )


def test_valid_docker_mcp_tool_config_parses_and_resolves() -> None:
    settings = validate_settings_mapping(
        {
            "profiles": {"worker": {"tools": ["docs"]}},
            "workspaces": {"research": {"profiles": ["worker"]}},
            "tools": {
                "docs": {
                    "type": "mcp",
                    "mcp": {
                        "runtime": "docker",
                        "image": "mcp/docs:latest",
                        "port": 8080,
                        "startup_timeout_seconds": 20,
                    },
                }
            },
        }
    )

    servers = merged_mcp_servers(settings, {})

    assert servers["docs"].type == "docker"
    assert servers["docs"].image == "mcp/docs:latest"
    assert servers["docs"].port == 8080
    assert servers["docs"].startup_timeout_seconds == pytest.approx(20)


def test_mcp_tool_provider_config_rejects_non_positive_startup_timeout() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        TypeAdapter(ToolConfig).validate_python(
            {
                "type": "mcp",
                "mcp": {
                    "runtime": "docker",
                    "image": "mcp/docs:latest",
                    "port": 8080,
                    "startup_timeout_seconds": 0,
                },
            }
        )


@pytest.mark.parametrize(
    ("mcp_config", "error"),
    [
        ({"runtime": "script", "port": 8080}, "Script MCP tools require 'command'"),
        ({"runtime": "script", "command": "uv"}, "Script MCP tools require 'port'"),
        ({"runtime": "docker", "port": 8080}, "Docker MCP tools require 'image'"),
        ({"runtime": "docker", "image": "mcp/example:latest"}, "Docker MCP tools require 'port'"),
        ({"runtime": "url"}, "URL MCP tools require 'url'"),
        ({"image": "mcp/example:latest"}, "Docker MCP tools require 'port'"),
        ({"port": 8080}, "Docker MCP tools require 'image'"),
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
