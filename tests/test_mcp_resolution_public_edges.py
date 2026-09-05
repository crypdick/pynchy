"""Public contracts for MCP server resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pynchy.config.api import BuiltinTool, McpTool, McpToolConfig, validate_settings_mapping
from pynchy.host.container_manager.mcp.resolution import (
    McpInstance,
    build_trust_map,
    configure_mcp_resolution_runtime,
    get_instance_id,
    merged_mcp_servers,
    resolve_all_instances,
    resolve_workspace_servers,
)
from pynchy.plugins.api import McpServerConfig
from pynchy.workspace.api import ResolvedWorkspaceConfig, ServiceTrustConfig


@dataclass
class FakeResolutionSettings:
    """Small settings contract for resolution tests without global config I/O."""

    tools: dict[str, object] = field(default_factory=dict)
    project_root: Path = Path("/project")
    workspaces: dict[str, object] = field(default_factory=dict)
    workspace_config: object | None = None

    def resolved_workspace_config(self, _folder: str) -> object | None:
        return self.workspace_config

    def workspace_names(self) -> list[str]:
        return list(self.workspaces)


def _workspace_config(*tools: str) -> ResolvedWorkspaceConfig:
    return ResolvedWorkspaceConfig(
        skills=[],
        tools=list(tools),
        repo=[],
        model=None,
        execution_mode="container",
        cwd=None,
        is_admin=False,
        contains_secrets=False,
    )


def _instance(server_name: str) -> McpInstance:
    return McpInstance(
        server_name=server_name,
        server_config=McpServerConfig(type="script", command="run", port=9000),
        kwargs={},
        instance_id=server_name,
        container_name=server_name,
        project_root=Path("/project"),
    )


def test_merged_servers_keeps_unmatched_plugin_servers_when_no_tool_overrides() -> None:
    settings = FakeResolutionSettings()
    server = McpServerConfig(type="url", url="https://example.test/mcp")

    assert merged_mcp_servers(settings, {"remote": server}) == {"remote": server}


def test_merged_servers_creates_a_server_for_a_tool_without_plugin_spec() -> None:
    settings = FakeResolutionSettings(
        tools={
            "browser": McpTool(
                type="mcp",
                mcp=McpToolConfig(runtime="docker", image="browser:latest", port=9100),
            )
        }
    )

    servers = merged_mcp_servers(settings, {})

    assert servers["browser"].type == "docker"
    assert servers["browser"].image == "browser:latest"
    assert servers["browser"].port == 9100


def test_merged_servers_ignores_non_mcp_tools() -> None:
    settings = FakeResolutionSettings(
        tools={
            "workspace": BuiltinTool(
                type="builtin",
                public_source=True,
            )
        }
    )
    server = McpServerConfig(type="url", url="https://example.test/mcp")

    assert merged_mcp_servers(settings, {"workspace": server}) == {"workspace": server}


def test_merged_servers_uses_default_runtime_for_explicit_mcp_fields() -> None:
    settings = FakeResolutionSettings(
        tools={
            "browser": McpTool(
                type="mcp",
                mcp=McpToolConfig(image="browser:latest", port=9100),
            )
        }
    )

    servers = merged_mcp_servers(settings, {})

    assert servers["browser"].type == "docker"
    assert servers["browser"].image == "browser:latest"
    assert servers["browser"].port == 9100


def test_resolve_workspace_servers_filters_unknown_tools_and_sorts_results(monkeypatch) -> None:
    settings = FakeResolutionSettings(
        workspace_config=_workspace_config("zeta", "unknown", "alpha"),
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.resolution._apply_tool_access",
        lambda _tools, resolved: (resolved, object()),
    )

    servers = resolve_workspace_servers(
        settings,
        {
            "alpha": McpServerConfig(type="url", url="https://alpha.test/mcp"),
            "zeta": McpServerConfig(type="url", url="https://zeta.test/mcp"),
            "unused": McpServerConfig(type="url", url="https://unused.test/mcp"),
        },
        "workspace",
    )

    assert servers == ["alpha", "zeta"]


def test_resolve_workspace_servers_returns_empty_without_workspace_config(monkeypatch) -> None:
    settings = FakeResolutionSettings()

    assert resolve_workspace_servers(settings, {}, "missing") == []


def test_instance_id_is_stable_for_kwargs_order() -> None:
    assert get_instance_id("browser", {"b": "2", "a": "1"}) == get_instance_id(
        "browser", {"a": "1", "b": "2"}
    )
    assert get_instance_id("browser", {}) == "browser"


def test_build_trust_map_prefers_configured_tool_trust() -> None:
    settings = validate_settings_mapping(
        {
            "tools": {
                "trusted": {
                    "type": "builtin",
                    "public_source": True,
                    "secret_data": False,
                    "public_sink": True,
                    "dangerous_writes": False,
                }
            }
        }
    )
    assert isinstance(settings.tools["trusted"], BuiltinTool)

    trust = build_trust_map(
        {"trusted": _instance("trusted")},
        {"trusted": ServiceTrustConfig(public_source=False)},
        settings,
    )

    assert trust["trusted"] == {
        "public_source": True,
        "secret_data": False,
        "public_sink": True,
        "dangerous_writes": False,
    }


def test_resolution_runtime_configuration_supplies_plugin_process_environment() -> None:
    captured: list[object] = []
    configure_mcp_resolution_runtime(
        apply_tool_access=lambda _tools, resolved: (resolved, object()),
        tool_process_environment=lambda tool: captured.append(tool) or {"TOKEN": "value"},
    )
    settings = FakeResolutionSettings(
        tools={"plugin": object()},
        workspaces={"workspace": object()},
        workspace_config=_workspace_config("plugin"),
    )

    state = resolve_all_instances(
        settings,
        {
            "plugin": McpServerConfig(type="script", command="run", port=9000),
        },
    )

    assert state.instances["plugin"].tool_environment == {"TOKEN": "value"}
    assert captured == [settings.tools["plugin"]]
