"""MCP server lifecycle manager — Docker/script on-demand, idle timeout, LiteLLM sync.

``config.toml`` is the single source of truth.  At boot, :meth:`McpManager.sync`
pushes MCP state to LiteLLM via its HTTP API.  Docker-based MCP containers and
script-based MCP subprocesses start on-demand when an agent first needs them
and stop after an idle timeout.

Adding a new MCP is as simple as adding a ``[mcp_servers.<name>]`` section to
``config.toml`` — no policy files, no editing ``litellm_config.yaml``.  Plugins
can also provide MCP servers via the ``pynchy_mcp_server_spec()`` hook.

Instance resolution (config expansion, kwargs, trust map) lives in
:mod:`_mcp_resolution`.  LiteLLM endpoint registration and team management
are in :mod:`_mcp_litellm`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pynchy.config import get_settings
from pynchy.host.container_manager.docker import (
    is_container_running,
    stop_container,
)
from pynchy.host.container_manager.mcp.lifecycle import (
    ensure_docker_running,
    ensure_script_running,
    terminate_process,
    warm_image_cache,
)
from pynchy.host.container_manager.mcp.litellm import (
    load_teams_cache,
    save_teams_cache,
    sync_mcp_endpoints,
    sync_teams,
)
from pynchy.host.container_manager.mcp.proxy import McpProxy
from pynchy.host.container_manager.mcp.resolution import (
    McpInstance,
    build_trust_map,
    get_instance_id,
    merged_mcp_servers,
    resolve_all_instances,
    resolve_kwargs,
    resolve_workspace_servers,
)
from pynchy.logger import logger
from pynchy.utils import create_background_task

if TYPE_CHECKING:
    from pynchy.config import Settings
    from pynchy.config.mcp import McpServerConfig
    from pynchy.host.container_manager.gateway import LiteLLMGateway
    from pynchy.types import ServiceTrustConfig


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceTeam:
    """Cached LiteLLM team + virtual key for a workspace."""

    team_id: str
    virtual_key: str


# ---------------------------------------------------------------------------
# McpManager
# ---------------------------------------------------------------------------


class McpManager:
    """Manages MCP servers: LiteLLM sync, Docker lifecycle, team provisioning.

    ``config.toml`` is the source of truth. At boot, this class syncs state to
    LiteLLM via HTTP API. Docker containers start on-demand and stop on idle.

    Instance resolution (what instances exist, for which workspaces) is
    delegated to :mod:`_mcp_resolution`.
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
        # Plugin-declared trust metadata — used by build_trust_map to populate
        # the proxy's trust map with real values instead of safe defaults.
        self._plugin_trust_defaults: dict[str, ServiceTrustConfig] = plugin_trust_defaults or {}
        self._instances: dict[str, McpInstance] = {}
        self._workspace_instances: dict[str, list[str]] = {}
        self._workspace_teams: dict[str, WorkspaceTeam] = {}
        self._teams_cache_path = settings.data_dir / "litellm" / "mcp_teams.json"
        self._idle_task: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[None] | None = None
        self._proxy = McpProxy()
        self._proxy_port: int = 0

    @property
    def _merged_mcp_servers(self) -> dict[str, McpServerConfig]:
        return merged_mcp_servers(self._settings, self._plugin_mcp_servers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def sync(self) -> None:
        """Sync config.toml MCP state to LiteLLM. Called once at boot."""
        all_servers = self._merged_mcp_servers
        if not all_servers:
            logger.info("No MCP servers configured — skipping MCP sync")
            return

        # 1. Resolve all instances needed across all workspaces
        state = resolve_all_instances(self._settings, all_servers)
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
        trust_map = build_trust_map(self._instances, self._plugin_trust_defaults)
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
        self._idle_task = create_background_task(self._idle_checker_loop(), name="mcp-idle-checker")

        # 7. Pre-pull Docker images in the background to warm the cache.
        #    Doesn't block boot — first on-demand start is just faster.
        self._warm_task = create_background_task(
            warm_image_cache(self._instances), name="mcp-warm-images"
        )

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
                if not await is_container_running(instance.container_name):
                    continue
                logger.info(
                    "Stopping idle MCP container",
                    instance_id=instance.instance_id,
                    idle_seconds=int(elapsed),
                )
                await stop_container(instance.container_name)

    async def stop_all(self) -> None:
        """Shutdown: stop all managed Docker containers and script subprocesses."""
        await self._proxy.stop()

        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        if self._warm_task is not None:
            self._warm_task.cancel()
            self._warm_task = None

        for instance in self._instances.values():
            if instance.server_config.type == "script":
                terminate_process(instance)
            elif instance.server_config.type == "docker":
                await stop_container(instance.container_name)

        logger.info("All MCP instances stopped")

    def resolve_workspace_servers(self, group_folder: str) -> list[str]:
        """Expand workspace's mcp_servers list (groups + names) into concrete server names."""
        return resolve_workspace_servers(self._settings, self._merged_mcp_servers, group_folder)

    def resolve_kwargs(self, group_folder: str, server_name: str) -> dict[str, str]:
        """Resolve per-workspace kwargs for an MCP server."""
        return resolve_kwargs(self._settings, group_folder, server_name)

    def get_instance_id(self, server_name: str, kwargs: dict[str, str]) -> str:
        """Compute instance ID: server_name + underscore + short hash of sorted kwargs."""
        return get_instance_id(server_name, kwargs)

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
