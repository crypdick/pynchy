"""MCP server lifecycle manager — instance resolution, Docker on-demand, idle timeout.

``config.toml`` is the single source of truth.  At boot, :meth:`McpManager.sync`
pushes MCP state to LiteLLM via its HTTP API.  Docker-based MCP containers start
on-demand when an agent first needs them and stop after an idle timeout.

Adding a new MCP is as simple as adding a ``[mcp_servers.<name>]`` section to
``config.toml`` — no policy files, no editing ``litellm_config.yaml``.

LiteLLM endpoint registration and team management are in
:mod:`_mcp_litellm` — this module handles instance resolution, Docker
lifecycle, and idle timeout only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings
from pynchy.container_runner._docker import (
    ensure_image,
    ensure_network,
    is_container_running,
    run_docker,
    wait_healthy,
)
from pynchy.container_runner._mcp_litellm import (
    load_teams_cache,
    save_teams_cache,
    sync_mcp_endpoints,
    sync_teams,
)
from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.config import Settings
    from pynchy.config_mcp import McpServerConfig
    from pynchy.container_runner.gateway import LiteLLMGateway

_NETWORK_NAME = "pynchy-litellm-net"
_MCP_CONTAINER_PREFIX = "pynchy-mcp"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class McpInstance:
    """A unique (server, kwargs) combination that maps to one Docker container
    or one URL endpoint registration in LiteLLM."""

    server_name: str
    server_config: McpServerConfig
    kwargs: dict[str, str]
    instance_id: str  # server_name + short hash of kwargs
    container_name: str  # Docker container name (for type=docker)
    last_activity: float = 0.0  # monotonic timestamp

    @property
    def endpoint_url(self) -> str:
        """URL that LiteLLM should use to reach this MCP server."""
        if self.server_config.type == "url":
            return self.server_config.url or ""
        # Docker: internal Docker network URL.
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

    def __init__(self, settings: Settings, gateway: LiteLLMGateway) -> None:
        self._settings = settings
        self._gateway = gateway
        self._instances: dict[str, McpInstance] = {}
        self._workspace_instances: dict[str, list[str]] = {}
        self._workspace_teams: dict[str, WorkspaceTeam] = {}
        self._teams_cache_path = settings.data_dir / "litellm" / "mcp_teams.json"
        self._idle_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def sync(self) -> None:
        """Sync config.toml MCP state to LiteLLM. Called once at boot."""
        if not self._settings.mcp_servers:
            logger.info("No MCP servers configured — skipping MCP sync")
            return

        # 1. Resolve all instances needed across all workspaces
        state = self._resolve_all_instances()
        self._instances = state.instances
        self._workspace_instances = state.workspace_instances

        if not self._instances:
            logger.info("No workspaces reference MCP servers — skipping MCP sync")
            return

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
        asyncio.create_task(self._warm_image_cache())

        logger.info(
            "MCP sync complete",
            instances=list(self._instances.keys()),
            workspaces=list(self._workspace_instances.keys()),
        )

    async def ensure_running(self, instance_id: str) -> None:
        """Start a Docker MCP instance if not already running.

        Called by the orchestrator before spawning an agent container.
        """
        instance = self._instances.get(instance_id)
        if instance is None:
            logger.warning("Unknown MCP instance", instance_id=instance_id)
            return

        if instance.server_config.type != "docker":
            return  # URL instances don't need starting

        instance.last_activity = time.monotonic()

        if is_container_running(instance.container_name):
            return

        logger.info(
            "Starting MCP container on-demand",
            instance_id=instance_id,
            container=instance.container_name,
            image=instance.server_config.image,
        )

        ensure_image(instance.server_config.image or "")
        ensure_network(_NETWORK_NAME)

        # Remove stale container
        run_docker("rm", "-f", instance.container_name, check=False)

        # Build container args
        cmd_args = list(instance.server_config.args)
        cmd_args.extend(_kwargs_to_args(instance.kwargs))

        # Publish port so the host can health-check the container.
        # endpoint_url uses the Docker-internal container name (for LiteLLM),
        # but the health check runs from the host which can't resolve those.
        port = instance.server_config.port
        publish_args = ["-p", f"{port}:{port}"] if port else []

        # Build -e flags from static env and env_forward on the server definition
        env_args = _build_env_args(instance.server_config)

        # Build -v flags from volumes, resolving relative host paths from project root
        volume_args: list[str] = []
        for vol in instance.server_config.volumes:
            host_path, sep, container_path = vol.partition(":")
            if sep and not Path(host_path).is_absolute():
                host_path = str(get_settings().project_root / host_path)
            resolved = f"{host_path}:{container_path}" if sep else vol
            Path(host_path).mkdir(parents=True, exist_ok=True)
            volume_args.extend(["-v", resolved])

        run_docker(
            "run", "-d",
            "--name", instance.container_name,
            "--network", _NETWORK_NAME,
            "--restart", "unless-stopped",
            *publish_args,
            *env_args,
            *volume_args,
            instance.server_config.image or "",
            *cmd_args,
        )  # fmt: skip

        # Health-check via localhost (host-side), not the Docker-internal name
        health_url = f"http://localhost:{port}" if port else instance.endpoint_url
        try:
            await wait_healthy(
                instance.container_name,
                health_url,
                any_non_5xx=True,
            )
        except (TimeoutError, RuntimeError):
            logger.error(
                "MCP container failed health check",
                instance_id=instance_id,
                container=instance.container_name,
            )
            raise

        logger.info("MCP container ready", instance_id=instance_id)

    async def stop_idle(self) -> None:
        """Stop Docker instances that exceeded their idle_timeout."""
        now = time.monotonic()
        for instance in list(self._instances.values()):
            if instance.server_config.type != "docker":
                continue
            if instance.server_config.idle_timeout == 0:
                continue  # Never auto-stop
            if not is_container_running(instance.container_name):
                continue

            elapsed = now - instance.last_activity
            if elapsed > instance.server_config.idle_timeout:
                logger.info(
                    "Stopping idle MCP container",
                    instance_id=instance.instance_id,
                    idle_seconds=int(elapsed),
                )
                run_docker("stop", "-t", "5", instance.container_name, check=False)
                run_docker("rm", "-f", instance.container_name, check=False)

    async def stop_all(self) -> None:
        """Shutdown: stop all managed Docker containers."""
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

        for instance in self._instances.values():
            if instance.server_config.type != "docker":
                continue
            run_docker("stop", "-t", "5", instance.container_name, check=False)
            run_docker("rm", "-f", instance.container_name, check=False)

        logger.info("All MCP containers stopped")

    def resolve_workspace_servers(self, group_folder: str) -> list[str]:
        """Expand workspace's mcp_servers list (groups + names) into concrete server names."""
        ws_config = self._settings.workspaces.get(group_folder)
        if not ws_config or not ws_config.mcp_servers:
            return []

        servers: set[str] = set()
        for entry in ws_config.mcp_servers:
            if entry == "all":
                servers.update(self._settings.mcp_servers.keys())
            elif entry in self._settings.mcp_groups:
                servers.update(self._settings.mcp_groups[entry])
            elif entry in self._settings.mcp_servers:
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
        """Compute instance ID: server_name + short hash of sorted kwargs.

        WARNING: LiteLLM rejects server names containing hyphens. The kwargs
        branch produces "name-hash" which will fail at registration. Use
        underscores if this path is ever exercised.
        """
        if not kwargs:
            return server_name
        kwargs_str = json.dumps(kwargs, sort_keys=True)
        short_hash = hashlib.sha256(kwargs_str.encode()).hexdigest()[:6]
        return f"{server_name}_{short_hash}"

    def get_workspace_key(self, group_folder: str) -> str | None:
        """Get cached LiteLLM virtual key for a workspace."""
        team = self._workspace_teams.get(group_folder)
        return team.virtual_key if team else None

    def get_workspace_instance_ids(self, group_folder: str) -> list[str]:
        """Get the list of MCP instance IDs for a workspace."""
        return self._workspace_instances.get(group_folder, [])

    # ------------------------------------------------------------------
    # Internal: resolution
    # ------------------------------------------------------------------

    def _resolve_all_instances(self) -> _SyncState:
        """Resolve all (server, kwargs) instances needed across all workspaces."""
        state = _SyncState()

        for folder, ws_config in self._settings.workspaces.items():
            if not ws_config.mcp_servers:
                continue

            servers = self.resolve_workspace_servers(folder)
            instance_ids: list[str] = []

            for server_name in servers:
                server_config = self._settings.mcp_servers.get(server_name)
                if server_config is None:
                    logger.warning(
                        "MCP server not found in config",
                        server=server_name,
                        workspace=folder,
                    )
                    continue

                kwargs = self.resolve_kwargs(folder, server_name)
                iid = self.get_instance_id(server_name, kwargs)

                if iid not in state.instances:
                    container_name = f"{_MCP_CONTAINER_PREFIX}-{iid}"
                    state.instances[iid] = McpInstance(
                        server_name=server_name,
                        server_config=server_config,
                        kwargs=kwargs,
                        instance_id=iid,
                        container_name=container_name,
                    )

                instance_ids.append(iid)

            if instance_ids:
                state.workspace_instances[folder] = instance_ids

        return state

    # ------------------------------------------------------------------
    # Internal: image warm-up
    # ------------------------------------------------------------------

    async def _warm_image_cache(self) -> None:
        """Pre-pull Docker images for all MCP instances in the background."""
        images = {
            inst.server_config.image
            for inst in self._instances.values()
            if inst.server_config.type == "docker" and inst.server_config.image
        }
        for image in images:
            try:
                await asyncio.to_thread(ensure_image, image)
                logger.info("Pre-pulled MCP image", image=image)
            except Exception:
                logger.warning("Failed to pre-pull MCP image", image=image)

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
# Helpers
# ---------------------------------------------------------------------------


def _kwargs_to_args(kwargs: dict[str, str]) -> list[str]:
    """Convert kwargs dict to Docker command args (``--key value`` pairs)."""
    args: list[str] = []
    for key, value in sorted(kwargs.items()):
        args.extend([f"--{key}", value])
    return args


def _build_env_args(config: McpServerConfig) -> list[str]:
    """Build ``-e KEY=VALUE`` Docker flags from ``env`` and ``env_forward``.

    ``env_forward`` is a ``{container_var: host_var}`` dict (normalized from
    list or dict form by the Pydantic validator).
    """
    args: list[str] = []
    for key, value in sorted(config.env.items()):
        args.extend(["-e", f"{key}={value}"])
    for container_var, host_var in sorted(config.env_forward.items()):
        value = os.environ.get(host_var)
        if value is None:
            logger.warning(
                "env_forward var not set on host — skipping",
                container_var=container_var,
                host_var=host_var,
            )
            continue
        args.extend(["-e", f"{container_var}={value}"])
    return args


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
