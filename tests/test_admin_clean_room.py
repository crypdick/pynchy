"""Tests for admin clean room policy — reject public_source MCPs in admin workspaces.

Admin workspaces are the most privileged and must never be corruption-tainted by
public_source MCPs. The validator rejects at startup if any MCP server reachable
from an admin workspace has public_source=True (or is undeclared in [services],
which defaults to public_source=True).
"""

import pytest
from pydantic import ValidationError

from pynchy.config import Settings
from pynchy.config_mcp import McpServerConfig
from pynchy.config_models import (
    ConnectionChatConfig,
    ConnectionsConfig,
    ServiceTrustTomlConfig,
    WhatsAppConnectionConfig,
    WorkspaceConfig,
)


def _base_connection() -> ConnectionsConfig:
    """Minimal valid connection for Settings construction."""
    return ConnectionsConfig(
        whatsapp={
            "wa1": WhatsAppConnectionConfig(
                auth_db_path="/tmp/test.db",
                chat={"mychat": ConnectionChatConfig()},
            )
        }
    )


def _ws(*, is_admin: bool = False, mcp_servers: list[str] | None = None) -> WorkspaceConfig:
    """Minimal workspace config with explicit non-exempt fields."""
    return WorkspaceConfig(
        chat="connection.whatsapp.wa1.chat.mychat",
        is_admin=is_admin,
        idle_terminate=True,
        mcp_servers=mcp_servers,
    )


def _docker_mcp(name: str = "test-mcp") -> dict[str, McpServerConfig]:
    """Single Docker MCP server definition."""
    return {name: McpServerConfig(type="docker", image="test:latest", port=8080)}


class TestAdminCleanRoomRejectsPublicSource:
    """Admin workspace with an MCP declared public_source=True must be rejected."""

    def test_rejects_explicit_public_source_true(self):
        with pytest.raises(ValidationError, match="public_source"):
            Settings(
                connection=_base_connection(),
                sandbox={
                    "admin-ws": _ws(is_admin=True, mcp_servers=["tainted-mcp"]),
                },
                mcp_servers=_docker_mcp("tainted-mcp"),
                services={
                    "tainted-mcp": ServiceTrustTomlConfig(
                        public_source=True,
                        secret_data=False,
                        public_sink=False,
                        dangerous_writes=False,
                    ),
                },
            )


class TestAdminCleanRoomRejectsUndeclared:
    """Admin workspace with an MCP missing from [services] must be rejected.

    An undeclared service defaults to public_source=True (maximally cautious),
    so it should also be blocked by the clean room policy.
    """

    def test_rejects_undeclared_service(self):
        with pytest.raises(ValidationError, match="public_source"):
            Settings(
                connection=_base_connection(),
                sandbox={
                    "admin-ws": _ws(is_admin=True, mcp_servers=["undeclared-mcp"]),
                },
                mcp_servers=_docker_mcp("undeclared-mcp"),
                # No services entry for undeclared-mcp → defaults to public_source=True
                services={},
            )


class TestAdminCleanRoomAllowsSafe:
    """Admin workspace with only public_source=False MCPs must be allowed."""

    def test_allows_safe_mcp(self):
        s = Settings(
            connection=_base_connection(),
            sandbox={
                "admin-ws": _ws(is_admin=True, mcp_servers=["safe-mcp"]),
            },
            mcp_servers=_docker_mcp("safe-mcp"),
            services={
                "safe-mcp": ServiceTrustTomlConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            },
        )
        assert s.workspaces["admin-ws"].is_admin is True
        assert s.workspaces["admin-ws"].mcp_servers == ["safe-mcp"]


class TestAdminCleanRoomGroupExpansion:
    """MCP group references in admin workspaces are expanded and checked."""

    def test_rejects_group_containing_public_source(self):
        with pytest.raises(ValidationError, match="public_source"):
            Settings(
                connection=_base_connection(),
                sandbox={
                    "admin-ws": _ws(is_admin=True, mcp_servers=["my-group"]),
                },
                mcp_servers={
                    "safe-mcp": McpServerConfig(type="docker", image="s:latest", port=8080),
                    "tainted-mcp": McpServerConfig(type="docker", image="t:latest", port=8081),
                },
                mcp_groups={"my-group": ["safe-mcp", "tainted-mcp"]},
                services={
                    "safe-mcp": ServiceTrustTomlConfig(
                        public_source=False,
                        secret_data=False,
                        public_sink=False,
                        dangerous_writes=False,
                    ),
                    "tainted-mcp": ServiceTrustTomlConfig(
                        public_source=True,
                        secret_data=False,
                        public_sink=False,
                        dangerous_writes=False,
                    ),
                },
            )

    def test_allows_group_all_safe(self):
        s = Settings(
            connection=_base_connection(),
            sandbox={
                "admin-ws": _ws(is_admin=True, mcp_servers=["my-group"]),
            },
            mcp_servers={
                "mcp-a": McpServerConfig(type="docker", image="a:latest", port=8080),
                "mcp-b": McpServerConfig(type="docker", image="b:latest", port=8081),
            },
            mcp_groups={"my-group": ["mcp-a", "mcp-b"]},
            services={
                "mcp-a": ServiceTrustTomlConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
                "mcp-b": ServiceTrustTomlConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            },
        )
        assert s.workspaces["admin-ws"].is_admin is True


class TestAdminCleanRoomAllKeyword:
    """The 'all' keyword expands to all MCP servers."""

    def test_rejects_all_when_any_public_source(self):
        with pytest.raises(ValidationError, match="public_source"):
            Settings(
                connection=_base_connection(),
                sandbox={
                    "admin-ws": _ws(is_admin=True, mcp_servers=["all"]),
                },
                mcp_servers={
                    "safe-mcp": McpServerConfig(type="docker", image="s:latest", port=8080),
                    "tainted-mcp": McpServerConfig(type="docker", image="t:latest", port=8081),
                },
                services={
                    "safe-mcp": ServiceTrustTomlConfig(
                        public_source=False,
                        secret_data=False,
                        public_sink=False,
                        dangerous_writes=False,
                    ),
                    "tainted-mcp": ServiceTrustTomlConfig(
                        public_source=True,
                        secret_data=False,
                        public_sink=False,
                        dangerous_writes=False,
                    ),
                },
            )


class TestAdminCleanRoomNonAdmin:
    """Non-admin workspaces are not subject to the clean room policy."""

    def test_non_admin_allows_public_source(self):
        s = Settings(
            connection=_base_connection(),
            sandbox={
                "normal-ws": _ws(is_admin=False, mcp_servers=["tainted-mcp"]),
            },
            mcp_servers=_docker_mcp("tainted-mcp"),
            services={
                "tainted-mcp": ServiceTrustTomlConfig(
                    public_source=True,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            },
        )
        assert s.workspaces["normal-ws"].is_admin is False
