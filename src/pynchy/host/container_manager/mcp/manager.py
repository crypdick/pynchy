"""MCP server lifecycle manager — Docker/script on-demand, idle timeout, LiteLLM sync.

``config.toml`` is the single source of truth.  At boot, :meth:`McpManager.sync`
pushes MCP state to LiteLLM via its HTTP API.  Docker-based MCP containers and
script-based MCP subprocesses start on-demand when an agent first needs them
and stop after an idle timeout.

Adding an MCP is done through a ``[tools.<name>]`` declaration with
``type = "mcp"``. Plugins can also provide MCP runtime specs via the
``pynchy_mcp_server_spec()`` hook.

Instance resolution (config expansion, kwargs, trust map) lives in
:mod:`_mcp_resolution`.  LiteLLM endpoint registration and team management
are in :mod:`_mcp_litellm`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config import Settings, get_settings
from pynchy.config.mcp import (
    McpServerConfig,  # noqa: TC001, RUF100 - beartype resolves MCP manager signatures at runtime.
)
from pynchy.host.container_manager.docker import (
    is_container_running,
    stop_container,
)
from pynchy.host.container_manager.gateway import LiteLLMGateway, resolve_container_host
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
    WorkspaceTeam,
    build_trust_map,
    merged_mcp_servers,
    resolve_all_instances,
)
from pynchy.host.container_manager.mcp.startup import McpStartupFailure, McpWorkspaceStartup
from pynchy.logger import logger
from pynchy.types import (
    ServiceTrustConfig,  # noqa: TC001, RUF100 - beartype resolves MCP manager signatures at runtime.
)
from pynchy.utils import create_background_task

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


_MCP_FAILURE_RETRY_SECONDS = 300.0


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
        self._instance_start_locks: dict[str, asyncio.Lock] = {}
        self._unavailable_until: dict[str, float] = {}
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
        self._unavailable_until.clear()

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
        trust_map = build_trust_map(
            self._instances,
            self._plugin_trust_defaults,
            settings=self._settings,
        )
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

    async def ensure_workspace_running(self, group_folder: str) -> McpWorkspaceStartup:
        """Ensure all MCP instances for a workspace are running.

        Starts assigned servers concurrently so independent failures do not
        add their readiness timeouts together. Failed instances are skipped
        until their retry cooldown elapses, while healthy siblings remain
        available to the agent.
        """
        instance_ids = self.get_workspace_instance_ids(group_folder)
        outcomes = await asyncio.gather(
            *(self._ensure_workspace_instance(iid, group_folder) for iid in instance_ids)
        )
        ready_instance_ids = tuple(iid for iid, ready, _failure in outcomes if ready)
        failures = tuple(failure for _iid, _ready, failure in outcomes if failure is not None)
        return McpWorkspaceStartup(ready_instance_ids=ready_instance_ids, failures=failures)

    async def ensure_running(self, instance_id: str) -> None:
        """Start an MCP instance (Docker container or host subprocess) if not running.

        Called by the orchestrator before spawning an agent container.
        """
        lock = self._instance_start_locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            await self._ensure_running_unlocked(instance_id)

    async def _ensure_workspace_instance(
        self, instance_id: str, group_folder: str
    ) -> tuple[str, bool, McpStartupFailure | None]:
        """Return the readiness outcome for one instance under its start lock."""
        lock = self._instance_start_locks.setdefault(instance_id, asyncio.Lock())
        async with lock:
            retry_at = self._unavailable_until.get(instance_id, 0.0)
            if retry_at > time.monotonic():
                return instance_id, False, None

            try:
                await self._ensure_running_unlocked(instance_id)
            except (TimeoutError, RuntimeError) as exc:
                instance = self._instances.get(instance_id)
                self._unavailable_until[instance_id] = time.monotonic() + _MCP_FAILURE_RETRY_SECONDS
                reason = "start timed out" if isinstance(exc, TimeoutError) else "failed to start"
                logger.warning(
                    "Failed to start MCP instance",
                    instance_id=instance_id,
                    group=group_folder,
                    error=str(exc),
                    retry_after_seconds=_MCP_FAILURE_RETRY_SECONDS,
                )
                return (
                    instance_id,
                    False,
                    McpStartupFailure(
                        instance_id=instance_id,
                        server_name=instance.server_name if instance else instance_id,
                        reason=reason,
                    ),
                )

            self._unavailable_until.pop(instance_id, None)
            return instance_id, True, None

    async def _ensure_running_unlocked(self, instance_id: str) -> None:
        """Perform an instance readiness check while the caller holds its lock."""
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

    def get_workspace_instance_ids(self, group_folder: str) -> list[str]:
        """Get the list of MCP instance IDs for a workspace."""
        return self._workspace_instances.get(
            workspace_config.static_workspace_folder(group_folder), []
        )

    def get_direct_server_configs(
        self,
        group_folder: str,
        invocation_ts: float = 0.0,
        *,
        instance_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[dict[str, str]]:
        """Get MCP connection configs for a workspace (routes through proxy).

        Returns a list of dicts suitable for the agent runner's MCP config.
        All traffic is routed through the MCP proxy for SecurityGate enforcement.
        """
        if instance_ids is None:
            instance_ids = self.get_workspace_instance_ids(group_folder)
        if not instance_ids or not self._proxy.port:
            return []

        host = resolve_container_host(get_settings().gateway.container_host)
        configs: list[dict[str, str]] = []
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
            except Exception:  # noqa: BLE001, RUF100 - idle checker is a background cleanup boundary.
                logger.exception("Error in MCP idle checker")


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------


@dataclass
class _McpManagerState:
    mcp_manager: McpManager | None = None


_state = _McpManagerState()


def get_mcp_manager() -> McpManager | None:
    """Return the active MCP manager, or ``None`` if not initialized."""
    return _state.mcp_manager


def set_mcp_manager(manager: McpManager | None) -> None:
    """Set the module-level MCP manager singleton."""
    _state.mcp_manager = manager
