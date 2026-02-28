"""MCP instance resolution — config expansion, kwargs, trust map.

Pure functions that resolve ``config.toml`` MCP entries into concrete
:class:`McpInstance` objects.  Extracted from :mod:`mcp_manager` so the
manager can focus on lifecycle orchestration and LiteLLM sync.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pynchy.logger import logger

if TYPE_CHECKING:
    import subprocess

    from pynchy.config import Settings
    from pynchy.config.mcp import McpServerConfig
    from pynchy.types import ServiceTrustConfig

_MCP_CONTAINER_PREFIX = "pynchy-mcp"


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
    port: int | None = None  # host-side port (auto-assigned for inject_workspace scripts)
    last_activity: float = 0.0  # monotonic timestamp
    process: subprocess.Popen | None = None  # tracked subprocess (for type=script)

    @property
    def endpoint_url(self) -> str:
        """URL that LiteLLM should use to reach this MCP server."""
        if self.server_config.type == "url":
            return self.server_config.url or ""
        if self.server_config.type == "script":
            # Script runs on host — LiteLLM reaches it via localhost.
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
class _SyncState:
    """Intermediate state built during sync — all instances and workspace mappings."""

    instances: dict[str, McpInstance] = field(default_factory=dict)
    workspace_instances: dict[str, list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resolution functions
# ---------------------------------------------------------------------------


def merged_mcp_servers(
    settings: Settings,
    plugin_mcp_servers: dict[str, McpServerConfig],
) -> dict[str, McpServerConfig]:
    """Config.toml servers + plugin-provided servers, with instance expansion.

    Instance expansion: for each template in ``mcp_server_instances``,
    the bare template is consumed (removed from result) and replaced by
    one entry per instance with auto-assigned port, chrome-profile volume
    mount, and PORT env var.
    """
    result = dict(plugin_mcp_servers)
    result.update(settings.mcp_servers)  # config.toml flat overrides

    # Expand template × instance pairs
    for template, instances in settings.mcp_server_instances.items():
        base = result.pop(template, None)
        if base is None:
            logger.warning(
                "No base spec for MCP template",
                template=template,
            )
            continue

        for i, (inst_name, overrides) in enumerate(sorted(instances.items())):
            qualified = f"{template}.{inst_name}"
            port = (base.port or 3000) + i
            chrome_profile = overrides.get("chrome_profile")

            # Build merged config updates
            updates: dict[str, Any] = {"port": port}

            if chrome_profile:
                vol = f"data/chrome-profiles/{chrome_profile}:/home/chrome"
                updates["volumes"] = list(base.volumes) + [vol]

            merged_env = dict(base.env)
            merged_env["PORT"] = str(port)
            updates["env"] = merged_env

            result[qualified] = base.model_copy(update=updates)

    return result


def resolve_workspace_servers(
    settings: Settings,
    all_servers: dict[str, McpServerConfig],
    group_folder: str,
) -> list[str]:
    """Expand workspace's mcp_servers list (groups + names) into concrete server names."""
    ws_config = settings.workspaces.get(group_folder)
    if not ws_config or not ws_config.mcp_servers:
        return []

    servers: set[str] = set()
    for entry in ws_config.mcp_servers:
        if entry == "all":
            servers.update(all_servers.keys())
        elif entry in settings.mcp_groups:
            servers.update(settings.mcp_groups[entry])
        elif entry in all_servers:
            servers.add(entry)
        else:
            logger.warning(
                "Unknown MCP server or group in workspace config",
                workspace=group_folder,
                entry=entry,
            )
    return sorted(servers)


def resolve_kwargs(settings: Settings, group_folder: str, server_name: str) -> dict[str, str]:
    """Resolve per-workspace kwargs for an MCP server.

    Expands presets and merges with explicit values.
    """
    ws_config = settings.workspaces.get(group_folder)
    if not ws_config:
        return {}

    raw_kwargs: dict[str, Any] = dict(ws_config.mcp.get(server_name, {}))

    # Extract and expand presets
    preset_names: list[str] = raw_kwargs.pop("presets", [])
    merged: dict[str, str] = {}

    for preset_name in preset_names:
        preset = settings.mcp_presets.get(preset_name, {})
        for key, value in preset.items():
            if key in merged:
                # Merge values with semicolons (for domain lists, etc.)
                merged[key] = f"{merged[key]};{value}"
            else:
                merged[key] = value

    # Explicit kwargs override/append to presets
    for key, value in raw_kwargs.items():
        if key in merged:
            merged[key] = f"{merged[key]};{str(value)}"
        else:
            merged[key] = str(value)

    return merged


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
    settings: Settings,
    all_servers: dict[str, McpServerConfig],
) -> _SyncState:
    """Resolve all (server, kwargs) instances needed across all workspaces.

    Auto-assigns host-side ports: first instance of a server gets
    ``cfg.port``, second gets ``cfg.port + 1``, etc.  This prevents port
    conflicts when ``inject_workspace`` creates multiple host-side
    instances of the same script-type server.
    """
    state = _SyncState()
    # Track how many instances we've created per server_name so we can
    # offset the host port for each additional instance.
    port_counters: dict[str, int] = {}

    for folder, ws_config in settings.workspaces.items():
        if not ws_config.mcp_servers:
            continue

        servers = resolve_workspace_servers(settings, all_servers, folder)
        instance_ids: list[str] = []

        for server_name in servers:
            server_config = all_servers.get(server_name)
            if server_config is None:
                logger.warning(
                    "MCP server not found in config",
                    server=server_name,
                    workspace=folder,
                )
                continue

            kwargs = resolve_kwargs(settings, folder, server_name)
            if server_config.inject_workspace:
                kwargs.setdefault("workspace", folder)
            iid = get_instance_id(server_name, kwargs)

            if iid not in state.instances:
                container_name = f"{_MCP_CONTAINER_PREFIX}-{iid}"
                offset = port_counters.get(server_name, 0)
                port_counters[server_name] = offset + 1
                base_port = server_config.port
                instance_port = (base_port + offset) if base_port is not None else None
                state.instances[iid] = McpInstance(
                    server_name=server_name,
                    server_config=server_config,
                    kwargs=kwargs,
                    instance_id=iid,
                    container_name=container_name,
                    port=instance_port,
                )

            instance_ids.append(iid)

        if instance_ids:
            state.workspace_instances[folder] = instance_ids

    return state


def build_trust_map(
    instances: dict[str, McpInstance],
    plugin_trust_defaults: dict[str, ServiceTrustConfig],
) -> dict[str, dict[str, Any]]:
    """Build trust metadata for each instance (used by proxy for fencing decisions).

    Priority: plugin defaults > safe fallback.
    """
    trust_map: dict[str, dict[str, Any]] = {}
    for iid, instance in instances.items():
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
