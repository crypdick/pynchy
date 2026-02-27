"""MCP server lifecycle manager — instance resolution, Docker/script on-demand, idle timeout.

``config.toml`` is the single source of truth.  At boot, :meth:`McpManager.sync`
pushes MCP state to LiteLLM via its HTTP API.  Docker-based MCP containers and
script-based MCP subprocesses start on-demand when an agent first needs them
and stop after an idle timeout.

Adding a new MCP is as simple as adding a ``[mcp_servers.<name>]`` section to
``config.toml`` — no policy files, no editing ``litellm_config.yaml``.  Plugins
can also provide MCP servers via the ``pynchy_mcp_server_spec()`` hook.

LiteLLM endpoint registration and team management are in
:mod:`_mcp_litellm` — this module handles instance resolution, Docker/script
lifecycle, and idle timeout only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings
from pynchy.container_runner._docker import (
    is_container_running,
    stop_container,
)
from pynchy.container_runner._mcp_lifecycle import (
    ensure_docker_running,
    ensure_script_running,
    terminate_process,
    warm_image_cache,
)
from pynchy.container_runner._mcp_litellm import (
    load_teams_cache,
    save_teams_cache,
    sync_mcp_endpoints,
    sync_teams,
)
from pynchy.container_runner._mcp_proxy import McpProxy
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.config import Settings
    from pynchy.config_mcp import McpServerConfig
    from pynchy.container_runner.gateway import LiteLLMGateway
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
# McpManager
# ---------------------------------------------------------------------------


class McpManager:
    """Manages MCP servers: LiteLLM sync, Docker lifecycle, team provisioning.

    ``config.toml`` is the source of truth. At boot, this class syncs state to
    LiteLLM via HTTP API. Docker containers start on-demand and stop on idle.
    """

    def __init__(
        self,
        settings: Settings,
        gateway: LiteLLMGateway,
        *,
        plugin_mcp_servers: dict[str, McpServerConfig] | None = None,
        plugin_trust_defaults: dict[str, ServiceTrustConfig] | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        # Plugin-provided MCP servers — merged with config.toml in _merged_mcp_servers.
        # Config.toml always wins on name collision (same semantics as workspace specs).
        self._plugin_mcp_servers: dict[str, McpServerConfig] = plugin_mcp_servers or {}
        # Plugin-declared trust metadata — used by _build_trust_map to populate
        # the proxy's trust map with real values instead of safe defaults.
        self._plugin_trust_defaults: dict[str, ServiceTrustConfig] = plugin_trust_defaults or {}
        self._instances: dict[str, McpInstance] = {}
        self._workspace_instances: dict[str, list[str]] = {}
        self._workspace_teams: dict[str, WorkspaceTeam] = {}
        self._teams_cache_path = settings.data_dir / "litellm" / "mcp_teams.json"
        self._idle_task: asyncio.Task[None] | None = None
        self._proxy = McpProxy()
        self._proxy_port: int = 0

    @property
    def _merged_mcp_servers(self) -> dict[str, McpServerConfig]:
        """Config.toml servers + plugin-provided servers, with instance expansion.

        Instance expansion: for each template in ``mcp_server_instances``,
        the bare template is consumed (removed from result) and replaced by
        one entry per instance with auto-assigned port, chrome-profile volume
        mount, and PORT env var.
        """
        result = dict(self._plugin_mcp_servers)
        result.update(self._settings.mcp_servers)  # config.toml flat overrides

        # Expand template × instance pairs
        for template, instances in self._settings.mcp_server_instances.items():
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def sync(self) -> None:
        """Sync config.toml MCP state to LiteLLM. Called once at boot."""
        if not self._merged_mcp_servers:
            logger.info("No MCP servers configured — skipping MCP sync")
            return

        # 1. Resolve all instances needed across all workspaces
        state = self._resolve_all_instances()
        self._instances = state.instances
        self._workspace_instances = state.workspace_instances

        if not self._instances:
            logger.info("No workspaces reference MCP servers — skipping MCP sync")
            return

        # Start MCP proxy — all MCP traffic routes through it for security enforcement
        instance_urls: dict[str, str] = {}
        for iid, inst in self._instances.items():
            cfg = inst.server_config
            if cfg.type == "url":
                instance_urls[iid] = cfg.url or ""
            elif inst.port is not None:
                instance_urls[iid] = f"http://localhost:{inst.port}"
        trust_map = self._build_trust_map()
        if instance_urls:
            self._proxy_port = await self._proxy.start(instance_urls, trust_map=trust_map)

        logger.info(
            "Syncing MCP state to LiteLLM",
            instance_count=len(self._instances),
            workspace_count=len(self._workspace_instances),
        )

        # 2. Load cached teams
        self._workspace_teams = load_teams_cache(self._teams_cache_path)

        # 3. Register MCP endpoints with LiteLLM
        await sync_mcp_endpoints(self._gateway, self._instances)

        # 4. Sync teams and virtual keys
        await sync_teams(self._gateway, self._workspace_instances, self._workspace_teams)

        # 5. Persist team cache
        save_teams_cache(self._teams_cache_path, self._workspace_teams)

        # 6. Start idle-timeout checker
        self._idle_task = asyncio.create_task(self._idle_checker_loop())

        # 7. Pre-pull Docker images in the background to warm the cache.
        #    Doesn't block boot — first on-demand start is just faster.
        asyncio.create_task(warm_image_cache(self._instances))

        logger.info(
            "MCP sync complete",
            instances=list(self._instances.keys()),
            workspaces=list(self._workspace_instances.keys()),
        )

    async def ensure_workspace_running(self, group_folder: str) -> None:
        """Ensure all MCP instances for a workspace are running.

        Calls :meth:`ensure_running` for each instance assigned to the
        workspace.  Failures are logged and skipped so one broken MCP
        server doesn't block the entire agent launch.
        """
        for iid in self.get_workspace_instance_ids(group_folder):
            try:
                await self.ensure_running(iid)
            except (TimeoutError, RuntimeError):
                logger.warning(
                    "Failed to start MCP instance",
                    instance_id=iid,
                    group=group_folder,
                )

    async def ensure_running(self, instance_id: str) -> None:
        """Start an MCP instance (Docker container or host subprocess) if not running.

        Called by the orchestrator before spawning an agent container.
        """
        instance = self._instances.get(instance_id)
        if instance is None:
            logger.warning("Unknown MCP instance", instance_id=instance_id)
            return

        if instance.server_config.type == "url":
            return  # URL instances don't need starting

        start = time.monotonic()
        instance.last_activity = start

        if instance.server_config.type == "script":
            await ensure_script_running(instance)
        else:
            await ensure_docker_running(instance)

        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > 500:
            logger.info(
                "MCP ensure_running slow",
                instance_id=instance_id,
                type=instance.server_config.type,
                elapsed_ms=round(elapsed_ms),
            )

    async def stop_idle(self) -> None:
        """Stop Docker/script instances that exceeded their idle_timeout."""
        now = time.monotonic()
        for instance in list(self._instances.values()):
            if instance.server_config.type not in ("docker", "script"):
                continue
            if instance.server_config.idle_timeout == 0:
                continue  # Never auto-stop

            elapsed = now - instance.last_activity
            if elapsed <= instance.server_config.idle_timeout:
                continue

            if instance.server_config.type == "script":
                if instance.process is None or instance.process.poll() is not None:
                    continue  # not running
                logger.info(
                    "Stopping idle MCP script",
                    instance_id=instance.instance_id,
                    idle_seconds=int(elapsed),
                )
                terminate_process(instance)
            else:
                if not is_container_running(instance.container_name):
                    continue
                logger.info(
                    "Stopping idle MCP container",
                    instance_id=instance.instance_id,
                    idle_seconds=int(elapsed),
                )
                stop_container(instance.container_name)

    async def stop_all(self) -> None:
        """Shutdown: stop all managed Docker containers and script subprocesses."""
        await self._proxy.stop()

        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

        for instance in self._instances.values():
            if instance.server_config.type == "script":
                terminate_process(instance)
            elif instance.server_config.type == "docker":
                stop_container(instance.container_name)

        logger.info("All MCP instances stopped")

    def resolve_workspace_servers(self, group_folder: str) -> list[str]:
        """Expand workspace's mcp_servers list (groups + names) into concrete server names."""
        ws_config = self._settings.workspaces.get(group_folder)
        if not ws_config or not ws_config.mcp_servers:
            return []

        merged_servers = self._merged_mcp_servers
        servers: set[str] = set()
        for entry in ws_config.mcp_servers:
            if entry == "all":
                servers.update(merged_servers.keys())
            elif entry in self._settings.mcp_groups:
                servers.update(self._settings.mcp_groups[entry])
            elif entry in merged_servers:
                servers.add(entry)
            else:
                logger.warning(
                    "Unknown MCP server or group in workspace config",
                    workspace=group_folder,
                    entry=entry,
                )
        return sorted(servers)

    def resolve_kwargs(self, group_folder: str, server_name: str) -> dict[str, str]:
        """Resolve per-workspace kwargs for an MCP server.

        Expands presets and merges with explicit values.
        """
        ws_config = self._settings.workspaces.get(group_folder)
        if not ws_config:
            return {}

        raw_kwargs: dict[str, Any] = dict(ws_config.mcp.get(server_name, {}))

        # Extract and expand presets
        preset_names: list[str] = raw_kwargs.pop("presets", [])
        merged: dict[str, str] = {}

        for preset_name in preset_names:
            preset = self._settings.mcp_presets.get(preset_name, {})
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

    def get_instance_id(self, server_name: str, kwargs: dict[str, str]) -> str:
        """Compute instance ID: server_name + underscore + short hash of sorted kwargs.

        Uses underscores as separator because LiteLLM rejects server names
        containing hyphens.
        """
        if not kwargs:
            return server_name
        kwargs_str = json.dumps(kwargs, sort_keys=True)
        short_hash = hashlib.sha256(kwargs_str.encode()).hexdigest()[:6]
        return f"{server_name}_{short_hash}"

    def get_workspace_instance_ids(self, group_folder: str) -> list[str]:
        """Get the list of MCP instance IDs for a workspace."""
        return self._workspace_instances.get(group_folder, [])

    def get_direct_server_configs(
        self, group_folder: str, invocation_ts: float = 0.0
    ) -> list[dict]:
        """Get MCP connection configs for a workspace (routes through proxy).

        Returns a list of dicts suitable for the agent runner's MCP config.
        All traffic is routed through the MCP proxy for SecurityGate enforcement.
        """
        instance_ids = self.get_workspace_instance_ids(group_folder)
        if not instance_ids or not self._proxy.port:
            return []

        host = get_settings().gateway.container_host
        configs: list[dict] = []
        for iid in instance_ids:
            instance = self._instances.get(iid)
            if instance is None:
                continue
            configs.append(
                {
                    "name": iid,
                    "url": f"http://{host}:{self._proxy.port}/mcp/{group_folder}/{invocation_ts}/{iid}",
                    "transport": instance.server_config.transport,
                }
            )
        return configs

    # ------------------------------------------------------------------
    # Internal: trust
    # ------------------------------------------------------------------

    def _build_trust_map(self) -> dict[str, dict[str, Any]]:
        """Build trust metadata for each instance (used by proxy for fencing decisions).

        Priority: plugin defaults > safe fallback.
        """
        trust_map: dict[str, dict[str, Any]] = {}
        for iid, instance in self._instances.items():
            plugin_trust = self._plugin_trust_defaults.get(instance.server_name)
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

    # ------------------------------------------------------------------
    # Internal: resolution
    # ------------------------------------------------------------------

    def _resolve_all_instances(self) -> _SyncState:
        """Resolve all (server, kwargs) instances needed across all workspaces.

        Auto-assigns host-side ports: first instance of a server gets
        ``cfg.port``, second gets ``cfg.port + 1``, etc.  This prevents port
        conflicts when ``inject_workspace`` creates multiple host-side
        instances of the same script-type server.
        """
        state = _SyncState()
        merged_servers = self._merged_mcp_servers
        # Track how many instances we've created per server_name so we can
        # offset the host port for each additional instance.
        port_counters: dict[str, int] = {}

        for folder, ws_config in self._settings.workspaces.items():
            if not ws_config.mcp_servers:
                continue

            servers = self.resolve_workspace_servers(folder)
            instance_ids: list[str] = []

            for server_name in servers:
                server_config = merged_servers.get(server_name)
                if server_config is None:
                    logger.warning(
                        "MCP server not found in config",
                        server=server_name,
                        workspace=folder,
                    )
                    continue

                kwargs = self.resolve_kwargs(folder, server_name)
                if server_config.inject_workspace:
                    kwargs.setdefault("workspace", folder)
                iid = self.get_instance_id(server_name, kwargs)

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

    # ------------------------------------------------------------------
    # Internal: idle checker
    # ------------------------------------------------------------------

    async def _idle_checker_loop(self) -> None:
        """Periodically check for idle MCP containers to stop."""
        while True:
            await asyncio.sleep(60)
            try:
                await self.stop_idle()
            except Exception:
                logger.exception("Error in MCP idle checker")


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_mcp_manager: McpManager | None = None


def get_mcp_manager() -> McpManager | None:
    """Return the active MCP manager, or ``None`` if not initialized."""
    return _mcp_manager


def set_mcp_manager(manager: McpManager | None) -> None:
    """Set the module-level MCP manager singleton."""
    global _mcp_manager
    _mcp_manager = manager
