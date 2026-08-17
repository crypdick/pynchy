"""MCP instance resolution — config expansion, kwargs, trust map.

Pure functions that resolve personalized MCP entries into concrete
:class:`McpInstance` objects.  Extracted from :mod:`mcp_manager` so the
manager can focus on lifecycle orchestration and LiteLLM sync.
"""

from __future__ import annotations

import hashlib
import json
import subprocess  # noqa: S404, TC003 - beartype resolves tracked MCP process annotations at runtime.
from collections.abc import (  # noqa: TC003 - beartype resolves MCP resolution annotations at runtime.
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from pathlib import (
    Path,  # noqa: TC003 - beartype resolves MCP instance annotations at runtime.
)
from typing import Any, Protocol, cast, runtime_checkable

from pynchy.plugins.api import McpServerConfig
from pynchy.runtime_names import runtime_container_name
from pynchy.workspace.api import (
    ServiceTrustConfig,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)


@runtime_checkable
class ResolvedMcpWorkspace(Protocol):
    @property
    def tools(self) -> Sequence[str]: ...


@runtime_checkable
class _McpRuntimeConfig(Protocol):
    runtime: str
    model_fields_set: set[str]

    def model_dump(self, **kwargs: object) -> dict[str, Any]: ...


@runtime_checkable
class _McpToolConfig(Protocol):
    type: str
    mcp: _McpRuntimeConfig


@runtime_checkable
class _ToolTrustConfig(Protocol):
    public_source: bool
    secret_data: bool
    public_sink: bool
    dangerous_writes: bool


class McpSettings(Protocol):
    @property
    def tools(self) -> Mapping[str, object]: ...

    @property
    def workspaces(self) -> Mapping[str, object]: ...

    @property
    def project_root(self) -> Path: ...

    def resolved_workspace_config(self, group_folder: str) -> object | None: ...

    def workspace_names(self) -> list[str]: ...


def _unconfigured_tool_access(
    _tools: Mapping[str, object], _resolved: object
) -> tuple[ResolvedMcpWorkspace, object]:
    raise RuntimeError("MCP tool access has not been composed")


def _unconfigured_tool_environment(_tool: object) -> dict[str, str]:
    raise RuntimeError("MCP tool environment has not been composed")


_apply_tool_access: Callable[
    [Mapping[str, object], object], tuple[ResolvedMcpWorkspace, object]
] = _unconfigured_tool_access
_tool_process_environment: Callable[[object], dict[str, str]] = _unconfigured_tool_environment


def configure_mcp_resolution_runtime(
    *,
    apply_tool_access: Callable[
        [Mapping[str, object], object], tuple[ResolvedMcpWorkspace, object]
    ],
    tool_process_environment: Callable[[object], dict[str, str]],
) -> None:
    """Bind config expansion helpers at host composition."""
    global _apply_tool_access, _tool_process_environment  # noqa: PLW0603 - one host process owns these MCP policy operations.
    _apply_tool_access = apply_tool_access
    _tool_process_environment = tool_process_environment


def _settings(settings: object) -> McpSettings:
    return cast("McpSettings", settings)


def _mcp_tool(tool: object) -> _McpToolConfig | None:
    if not isinstance(tool, _McpToolConfig) or tool.type != "mcp":
        return None
    return tool


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class McpInstance:
    """A unique (server, kwargs) combination that maps to one Docker container,
    one host subprocess, or one URL endpoint registration in LiteLLM."""

    server_name: str
    server_config: McpServerConfig
    kwargs: dict[str, str]
    instance_id: str  # server_name + short hash of kwargs
    container_name: str  # Docker container name (for type=docker)
    project_root: Path
    port: int | None = None  # host-side port (auto-assigned for host-process instances)
    tool_environment: dict[str, str] = field(default_factory=dict)
    last_activity: float = 0.0  # monotonic timestamp
    process: subprocess.Popen[bytes] | None = None  # tracked subprocess (for type=script/stdio)
    process_marker: str | None = None
    process_record_path: Path | None = None

    @property
    def endpoint_url(self) -> str:
        """URL that LiteLLM should use to reach this MCP server."""
        if self.server_config.type == "url":
            return self.server_config.url or ""
        if self.server_config.type in ("script", "stdio"):
            # Host processes run on the Pynchy host — LiteLLM reaches localhost.
            # Uses instance port (unique per workspace) over config port.
            base = f"http://localhost:{self.port}"
            if self.server_config.transport in ("http", "streamable_http"):
                return f"{base}/mcp"
            return base
        # Docker: internal Docker network URL (no host port conflict).
        # Streamable HTTP uses /mcp path; SSE uses bare host:port.
        base = f"http://{self.container_name}:{self.server_config.port}"
        if self.server_config.transport in ("http", "streamable_http"):
            return f"{base}/mcp"
        return base


@dataclass
class WorkspaceTeam:
    """Cached LiteLLM team + virtual key for a workspace."""

    team_id: str
    virtual_key: str


@dataclass
class _SyncState:
    """Intermediate state built during sync — all instances and workspace mappings."""

    instances: dict[str, McpInstance] = field(default_factory=dict)
    workspace_instances: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resolution functions
# ---------------------------------------------------------------------------


def _resolved_workspace_config(
    settings: object,
    group_folder: str,
) -> ResolvedMcpWorkspace | None:
    """Resolve workspace config before reading selected MCP tool declarations."""
    settings_view = _settings(settings)
    resolved = settings_view.resolved_workspace_config(group_folder)
    if resolved is None:
        return None
    return _apply_tool_access(settings_view.tools, resolved)[0]


def _mcp_runtime_updates(tool: _McpToolConfig) -> dict[str, Any]:
    fields = set(tool.mcp.model_fields_set)
    fields.discard("credentials_path")
    updates = tool.mcp.model_dump(include=fields, exclude_none=True)
    if "runtime" in updates:
        updates["type"] = updates.pop("runtime")
    return updates


def merged_mcp_servers(
    settings: object,
    plugin_mcp_servers: dict[str, McpServerConfig],
) -> dict[str, McpServerConfig]:
    """Tool-declared MCP runtime configs + plugin-provided servers.

    Tool declarations override matching plugin-provided server definitions.
    """
    result = dict(plugin_mcp_servers)

    settings_view = _settings(settings)
    for name, configured_tool in settings_view.tools.items():
        tool = _mcp_tool(configured_tool)
        if tool is None:
            continue
        updates = _mcp_runtime_updates(tool)
        base = result.get(name)
        if base is None:
            if "type" not in updates:
                updates["type"] = tool.mcp.runtime
            result[name] = McpServerConfig(**updates)
            continue
        result[name] = base.model_copy(
            update=updates,
        )

    return result


def resolve_workspace_servers(
    settings: object,
    all_servers: dict[str, McpServerConfig],
    group_folder: str,
) -> list[str]:
    """Expand resolved MCP tool names into concrete server names."""
    settings_view = _settings(settings)
    ws_config = _resolved_workspace_config(settings_view, group_folder)
    if not ws_config:
        return []

    return sorted(name for name in ws_config.tools if name in all_servers)


def get_instance_id(server_name: str, kwargs: dict[str, str]) -> str:
    """Compute instance ID: server_name + underscore + short hash of sorted kwargs.

    Uses underscores as separator because LiteLLM rejects server names
    containing hyphens.
    """
    if not kwargs:
        return server_name
    kwargs_str = json.dumps(kwargs, sort_keys=True)
    short_hash = hashlib.sha256(kwargs_str.encode()).hexdigest()[:6]
    return f"{server_name}_{short_hash}"


def resolve_all_instances(
    settings: object,
    all_servers: dict[str, McpServerConfig],
) -> _SyncState:
    """Resolve all (server, kwargs) instances needed across all workspaces.

    Auto-assigns host-side ports: first instance of a server gets
    ``cfg.port``, second gets ``cfg.port + 1``, etc.  This prevents port
    conflicts when ``inject_workspace`` creates multiple host-side
    instances of the same host-process server.
    """
    state = _SyncState()
    # Track how many instances we've created per server_name so we can
    # offset the host port for each additional instance.
    port_counters: dict[str, int] = {}
    settings_view = _settings(settings)
    server = getattr(settings_view, "server", None)
    gateway = getattr(settings_view, "gateway", None)
    assigned_ports = {
        getattr(server, "port", 0),
        getattr(gateway, "port", 0),
        getattr(gateway, "mcp_proxy_port", 0),
    }
    assigned_ports.discard(0)

    # Semantic child workspaces own policy independently from their physical
    # roots, so they must receive their own selected MCP instances too.
    for folder in settings_view.workspace_names():
        servers = resolve_workspace_servers(settings, all_servers, folder)
        if not servers:
            continue
        instance_ids: list[str] = []

        for server_name in servers:
            server_config = all_servers[server_name]
            kwargs: dict[str, str] = {}
            if server_config.inject_workspace:
                kwargs.setdefault("workspace", folder)
            iid = get_instance_id(server_name, kwargs)

            if iid not in state.instances:
                container_name = runtime_container_name(f"mcp-{iid}")
                tool = settings_view.tools.get(server_name)
                offset = port_counters.get(server_name, 0)
                base_port = server_config.port
                instance_port = (base_port + offset) if base_port is not None else None
                while (
                    server_config.type in ("script", "stdio")
                    and instance_port is not None
                    and instance_port in assigned_ports
                ):
                    offset += 1
                    instance_port = base_port + offset if base_port is not None else None
                port_counters[server_name] = offset + 1
                if server_config.type in ("script", "stdio") and instance_port is not None:
                    assigned_ports.add(instance_port)
                state.instances[iid] = McpInstance(
                    server_name=server_name,
                    server_config=server_config,
                    kwargs=kwargs,
                    instance_id=iid,
                    container_name=container_name,
                    project_root=settings_view.project_root,
                    port=instance_port,
                    tool_environment=_tool_process_environment(tool) if tool is not None else {},
                )

            instance_ids.append(iid)

        state.workspace_instances[folder] = instance_ids

    return state


def build_trust_map(
    instances: dict[str, McpInstance],
    plugin_trust_defaults: dict[str, ServiceTrustConfig],
    settings: object | None = None,
) -> dict[str, dict[str, Any]]:
    """Build trust metadata for each instance (used by proxy for fencing decisions).

    Priority: configured tool trust, plugin defaults, else a safe default.
    """
    trust_map: dict[str, dict[str, Any]] = {}
    for iid, instance in instances.items():
        tool = settings and _settings(settings).tools.get(instance.server_name)
        trust = tool if isinstance(tool, _ToolTrustConfig) else None
        if trust is not None:
            trust_map[iid] = {
                "public_source": trust.public_source,
                "secret_data": trust.secret_data,
                "public_sink": trust.public_sink,
                "dangerous_writes": trust.dangerous_writes,
            }
            continue

        plugin_trust = plugin_trust_defaults.get(instance.server_name)
        if plugin_trust:
            trust_map[iid] = {
                "public_source": plugin_trust.public_source,
                "secret_data": plugin_trust.secret_data,
                "public_sink": plugin_trust.public_sink,
                "dangerous_writes": plugin_trust.dangerous_writes,
            }
        else:
            trust_map[iid] = {"public_source": False}
    return trust_map
