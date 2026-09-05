"""Configuration-boundary coverage for MCP instance resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pynchy.config.api import McpTool, McpToolConfig
from pynchy.host.container_manager.mcp.resolution import (
    McpInstance,
    build_trust_map,
    configure_mcp_resolution_runtime,
    merged_mcp_servers,
    resolve_all_instances,
)
from pynchy.plugins.api import McpServerConfig
from pynchy.workspace.api import ResolvedWorkspaceConfig


@dataclass
class _Settings:
    tools: dict[str, object] = field(default_factory=dict)
    workspaces: dict[str, object] = field(default_factory=dict)
    configs: dict[str, ResolvedWorkspaceConfig | None] = field(default_factory=dict)
    project_root: Path = Path("/project")

    def resolved_workspace_config(self, folder: str) -> ResolvedWorkspaceConfig | None:
        return self.configs.get(folder)

    def workspace_names(self) -> list[str]:
        return list(self.configs)


def _config(*tools: str) -> ResolvedWorkspaceConfig:
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


def test_tool_declared_runtime_overrides_plugin_server() -> None:
    settings = _Settings(
        tools={
            "browser": McpTool(
                type="mcp",
                mcp=McpToolConfig(
                    runtime="docker",
                    image="browser:new",
                    port=9100,
                    env={"MODE": "new"},
                ),
            )
        }
    )
    base = McpServerConfig(type="docker", image="browser:old", port=9000)

    result = merged_mcp_servers(settings, {"browser": base})

    assert result["browser"].image == "browser:new"
    assert result["browser"].port == 9100
    assert result["browser"].env == {"MODE": "new"}


def test_stdio_tool_config_accepts_streamable_http_transport() -> None:
    config = McpToolConfig(
        runtime="stdio",
        command="stdio-backend",
        port=9100,
        transport="streamable_http",
    )

    assert config.runtime == "stdio"
    assert config.transport == "streamable_http"


def test_resolve_all_instances_assigns_unique_ports_across_server_names() -> None:
    settings = _Settings(
        tools={"first": object(), "second": object()},
        workspaces={"workspace": object()},
        configs={"workspace": _config("first", "second")},
    )
    configure_mcp_resolution_runtime(
        apply_tool_access=lambda _tools, resolved: (resolved, object()),
        tool_process_environment=lambda _tool: {},
    )
    servers = {
        "first": McpServerConfig(type="script", command="run-first", port=9000),
        "second": McpServerConfig(type="script", command="run-second", port=9000),
    }

    state = resolve_all_instances(settings, servers)

    assert {instance.port for instance in state.instances.values()} == {9000, 9001}
    assert state.workspace_instances["workspace"] == ["first", "second"]


def test_trust_map_defaults_unconfigured_instance_to_private_source() -> None:
    instance = McpInstance(
        server_name="browser",
        server_config=McpServerConfig(type="url", url="https://browser.test/mcp"),
        kwargs={},
        instance_id="browser",
        container_name="browser",
        project_root=Path("/project"),
    )

    assert build_trust_map({"browser": instance}, {}) == {"browser": {"public_source": False}}
