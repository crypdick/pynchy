"""Tests for admin clean-room policy over resolved tools."""

import pytest
from pydantic import ValidationError

from pynchy.config.api import ProfileConfig, WorkspaceConfig, validate_settings_mapping


def _settings_data(
    *,
    profile: ProfileConfig,
    tools: dict[str, dict],
    workspace_profile: str = "admin",
) -> dict:
    return {
        "profiles": {workspace_profile: profile.model_dump(exclude_defaults=True)},
        "workspaces": {"admin-ws": WorkspaceConfig(profiles=[workspace_profile]).model_dump()},
        "tools": tools,
    }


def _mcp_tool(*, public_source=True) -> dict:
    return {
        "type": "mcp",
        "public_source": public_source,
        "mcp": {"runtime": "docker", "image": "mcp/admin-test:latest", "port": 8080},
    }


class TestAdminCleanRoomRejectsPublicSource:
    def test_rejects_explicit_public_source_true(self):
        with pytest.raises(ValidationError, match="public_source"):
            validate_settings_mapping(
                _settings_data(
                    profile=ProfileConfig(is_admin=True, tools=["tainted-mcp"]),
                    tools={"tainted-mcp": _mcp_tool(public_source=True)},
                )
            )

    def test_rejects_forbidden_public_source(self):
        with pytest.raises(ValidationError, match="public_source"):
            validate_settings_mapping(
                _settings_data(
                    profile=ProfileConfig(is_admin=True, tools=["forbidden-mcp"]),
                    tools={"forbidden-mcp": _mcp_tool(public_source="forbidden")},
                )
            )

    def test_rejects_default_public_source(self):
        with pytest.raises(ValidationError, match="public_source"):
            validate_settings_mapping(
                _settings_data(
                    profile=ProfileConfig(is_admin=True, tools=["default-mcp"]),
                    tools={
                        "default-mcp": {
                            "type": "mcp",
                            "mcp": {
                                "runtime": "docker",
                                "image": "mcp/admin-test:latest",
                                "port": 8080,
                            },
                        }
                    },
                )
            )


class TestAdminCleanRoomRejectsUnknown:
    def test_rejects_unknown_tool_reference(self):
        with pytest.raises(ValidationError, match="unknown tool"):
            validate_settings_mapping(
                _settings_data(
                    profile=ProfileConfig(is_admin=True, tools=["missing-mcp"]),
                    tools={},
                )
            )


class TestAdminCleanRoomAllowsSafe:
    def test_allows_safe_mcp(self):
        s = validate_settings_mapping(
            _settings_data(
                profile=ProfileConfig(is_admin=True, tools=["safe-mcp"]),
                tools={"safe-mcp": _mcp_tool(public_source=False)},
            )
        )
        resolved = s.resolved_workspace_config("admin-ws")
        assert resolved is not None
        assert resolved.is_admin is True
        assert resolved.tools == ["safe-mcp"]

    def test_allows_composed_safe_tools(self):
        s = validate_settings_mapping(
            {
                "profiles": {
                    "base": {"tools": ["mcp-a"]},
                    "admin": {"includes": ["base"], "is_admin": True, "tools": ["mcp-b"]},
                },
                "workspaces": {"admin-ws": {"profiles": ["admin"]}},
                "tools": {
                    "mcp-a": _mcp_tool(public_source=False),
                    "mcp-b": _mcp_tool(public_source=False),
                },
            }
        )
        resolved = s.resolved_workspace_config("admin-ws")
        assert resolved is not None
        assert resolved.tools == ["mcp-a", "mcp-b"]


class TestAdminCleanRoomNonAdmin:
    def test_non_admin_allows_public_source(self):
        s = validate_settings_mapping(
            {
                "profiles": {"normal": {"is_admin": False, "tools": ["tainted-mcp"]}},
                "workspaces": {"normal-ws": {"profiles": ["normal"]}},
                "tools": {"tainted-mcp": _mcp_tool(public_source=True)},
            }
        )
        resolved = s.resolved_workspace_config("normal-ws")
        assert resolved is not None
        assert resolved.is_admin is False
