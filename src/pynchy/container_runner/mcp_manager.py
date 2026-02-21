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
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pynchy.config import get_settings
from pynchy.container_runner._docker import (
    ensure_image,
    ensure_network,
    is_container_running,
    remove_container,
    run_docker,
    stop_container,
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
    """A unique (server, kwargs) combination that maps to one Docker container,
    one host subprocess, or one URL endpoint registration in LiteLLM."""

    server_name: str
    server_config: McpServerConfig
    kwargs: dict[str, str]
    instance_id: str  # server_name + short hash of kwargs
    container_name: str  # Docker container name (for type=docker)
    last_activity: float = 0.0  # monotonic timestamp
    process: subprocess.Popen | None = None  # tracked subprocess (for type=script)

    @property
    def endpoint_url(self) -> str:
        """URL that LiteLLM should use to reach this MCP server."""
        if self.server_config.type == "url":
            return self.server_config.url or ""
        if self.server_config.type == "script":
            # Script runs on host — LiteLLM reaches it via localhost.
            base = f"http://localhost:{self.server_config.port}"
            if self.server_config.transport in ("http", "streamable_http"):
                return f"{base}/mcp"
            return base
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

    def __init__(
        self,
        settings: Settings,
        gateway: LiteLLMGateway,
        *,
        plugin_mcp_servers: dict[str, McpServerConfig] | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = gateway
        # Plugin-provided MCP servers — merged with config.toml in _merged_mcp_servers.
        # Config.toml always wins on name collision (same semantics as workspace specs).
        self._plugin_mcp_servers: dict[str, McpServerConfig] = plugin_mcp_servers or {}
        self._instances: dict[str, McpInstance] = {}
        self._workspace_instances: dict[str, list[str]] = {}
        self._workspace_teams: dict[str, WorkspaceTeam] = {}
        self._teams_cache_path = settings.data_dir / "litellm" / "mcp_teams.json"
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def _merged_mcp_servers(self) -> dict[str, McpServerConfig]:
        """Config.toml servers + plugin-provided servers (config wins on collision)."""
        merged = dict(self._plugin_mcp_servers)
        merged.update(self._settings.mcp_servers)  # config.toml wins
        return merged

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
            await self._ensure_script_running(instance)
        else:
            await self._ensure_docker_running(instance)

        elapsed_ms = (time.monotonic() - start) * 1000
        if elapsed_ms > 500:
            logger.info(
                "MCP ensure_running slow",
                instance_id=instance_id,
                type=instance.server_config.type,
                elapsed_ms=round(elapsed_ms),
            )

    async def _ensure_script_running(self, instance: McpInstance) -> None:
        """Start a script MCP subprocess if not already running."""
        if instance.process is not None and instance.process.poll() is None:
            return  # still alive

        cfg = instance.server_config
        cmd = [cfg.command or "", *cfg.args]
        cmd.extend(_kwargs_to_args(instance.kwargs))

        # Merge env: inherit host env + static env + env_forward
        merged_env = {**os.environ, **cfg.env}
        merged_env.update(_resolve_env_forward(cfg.env_forward))

        logger.info(
            "Starting MCP script on-demand",
            instance_id=instance.instance_id,
            command=cmd,
        )

        instance.process = subprocess.Popen(
            cmd,
            env=merged_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # own process group for clean shutdown
        )

        # Health-check via localhost
        health_url = f"http://localhost:{cfg.port}"
        try:
            await wait_healthy(
                instance.instance_id,
                health_url,
                any_non_5xx=True,
                process=instance.process,
            )
        except (TimeoutError, RuntimeError):
            stderr_tail = ""
            if instance.process.stderr:
                with contextlib.suppress(OSError, ValueError):
                    stderr_tail = instance.process.stderr.read(2000).decode(errors="replace")
            logger.error(
                "MCP script failed health check",
                instance_id=instance.instance_id,
                stderr=stderr_tail,
            )
            _terminate_process(instance)
            raise

        logger.info("MCP script ready", instance_id=instance.instance_id)

    async def _ensure_docker_running(self, instance: McpInstance) -> None:
        """Start a Docker MCP container if not already running."""
        if is_container_running(instance.container_name):
            return

        logger.info(
            "Starting MCP container on-demand",
            instance_id=instance.instance_id,
            container=instance.container_name,
            image=instance.server_config.image,
        )

        _ensure_mcp_image(instance.server_config)
        ensure_network(_NETWORK_NAME)

        # Remove stale container
        remove_container(instance.container_name)

        # Build container args
        cmd_args = list(instance.server_config.args)
        cmd_args.extend(_kwargs_to_args(instance.kwargs))

        # Publish port so the host can health-check the container.
        # endpoint_url uses the Docker-internal container name (for LiteLLM),
        # but the health check runs from the host which can't resolve those.
        port = instance.server_config.port
        publish_args = ["-p", f"{port}:{port}"] if port else []
        for extra_port in instance.server_config.extra_ports:
            publish_args.extend(["-p", f"{extra_port}:{extra_port}"])

        # Build -e flags from static env and env_forward on the server definition
        env_args = _build_env_args(instance.server_config)

        # Build -v flags from volumes, resolving relative host paths from project root.
        # Docker named volumes (no "/" or "." in the name, e.g. "mcp-gdrive:/data")
        # are passed through as-is; only host paths get resolved and mkdir'd.
        # Expand {key} placeholders using instance kwargs (e.g.,
        # "groups/{workspace}:/workspace" → "groups/research:/workspace").
        volume_args: list[str] = []
        for vol in instance.server_config.volumes:
            for key, value in instance.kwargs.items():
                vol = vol.replace(f"{{{key}}}", value)
            host_path, sep, container_path = vol.partition(":")
            if sep and "/" not in host_path and not host_path.startswith("."):
                # Docker named volume — pass through without resolution
                volume_args.extend(["-v", vol])
            elif sep and not Path(host_path).is_absolute():
                host_path = str(get_settings().project_root / host_path)
                _ensure_mount_parent(host_path)
                volume_args.extend(["-v", f"{host_path}:{container_path}"])
            else:
                if sep:
                    _ensure_mount_parent(host_path)
                volume_args.extend(["-v", vol])

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
                instance_id=instance.instance_id,
                container=instance.container_name,
            )
            # Clean up the failed container (matches script path which
            # calls _terminate_process before re-raising).
            stop_container(instance.container_name)
            raise

        logger.info("MCP container ready", instance_id=instance.instance_id)

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
                _terminate_process(instance)
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
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

        for instance in self._instances.values():
            if instance.server_config.type == "script":
                _terminate_process(instance)
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

    def get_workspace_key(self, group_folder: str) -> str | None:
        """Get cached LiteLLM virtual key for a workspace."""
        team = self._workspace_teams.get(group_folder)
        return team.virtual_key if team else None

    def get_workspace_instance_ids(self, group_folder: str) -> list[str]:
        """Get the list of MCP instance IDs for a workspace."""
        return self._workspace_instances.get(group_folder, [])

    def get_direct_server_configs(self, group_folder: str) -> list[dict]:
        """Get direct MCP connection configs for a workspace (bypasses LiteLLM).

        Returns a list of dicts suitable for the agent runner's MCP config:
        ``[{"name": "gdrive", "url": "http://host.docker.internal:3000", "transport": "sse"}, ...]``
        """
        instance_ids = self.get_workspace_instance_ids(group_folder)
        configs: list[dict] = []
        for iid in instance_ids:
            instance = self._instances.get(iid)
            if instance is None:
                continue
            cfg = instance.server_config
            if cfg.type == "url":
                configs.append(
                    {
                        "name": iid,
                        "url": cfg.url or "",
                        "transport": cfg.transport,
                    }
                )
            elif cfg.port is not None:
                # Docker/script containers publish ports to localhost.
                # Agent containers reach the host via host.docker.internal.
                host = get_settings().gateway.container_host
                configs.append(
                    {
                        "name": iid,
                        "url": f"http://{host}:{cfg.port}",
                        "transport": cfg.transport,
                    }
                )
        return configs

    # ------------------------------------------------------------------
    # Internal: resolution
    # ------------------------------------------------------------------

    def _resolve_all_instances(self) -> _SyncState:
        """Resolve all (server, kwargs) instances needed across all workspaces."""
        state = _SyncState()
        merged_servers = self._merged_mcp_servers

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
        """Pre-pull/build Docker images for all MCP instances in the background."""
        seen: set[str] = set()
        for inst in self._instances.values():
            cfg = inst.server_config
            if cfg.type != "docker" or not cfg.image or cfg.image in seen:
                continue
            seen.add(cfg.image)
            try:
                await asyncio.to_thread(_ensure_mcp_image, cfg)
                logger.info("Warmed MCP image cache", image=cfg.image)
            except Exception:
                logger.warning("Failed to warm MCP image", image=cfg.image)

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


def _ensure_mcp_image(config: McpServerConfig) -> None:
    """Ensure the MCP Docker image exists — build from local Dockerfile or pull.

    When ``config.dockerfile`` is set and the image isn't already local,
    builds it from the specified Dockerfile. Otherwise falls back to pulling
    from a registry via :func:`ensure_image`.
    """
    image = config.image or ""
    if config.dockerfile:
        # Check if image already exists locally
        result = run_docker("image", "inspect", image, check=False)
        if result.returncode == 0:
            return
        # Build from local Dockerfile
        project_root = str(get_settings().project_root)
        dockerfile_path = str(get_settings().project_root / config.dockerfile)
        logger.info(
            "Building MCP image from local Dockerfile",
            image=image,
            dockerfile=config.dockerfile,
        )
        run_docker(
            "build", "-t", image,
            "-f", dockerfile_path,
            project_root,
            timeout=300,
        )  # fmt: skip
        logger.info("MCP image built", image=image)
    else:
        ensure_image(image)


def _ensure_mount_parent(host_path: str) -> None:
    """Ensure mount source exists — mkdir for directories, parent-mkdir for files."""
    p = Path(host_path)
    if p.exists():
        return  # already exists (file or directory)
    # Heuristic: paths with file extensions are files, others are directories.
    if p.suffix:
        p.parent.mkdir(parents=True, exist_ok=True)
    else:
        p.mkdir(parents=True, exist_ok=True)


def _terminate_process(instance: McpInstance) -> None:
    """SIGTERM a script MCP subprocess, escalating to SIGKILL after 5s."""
    proc = instance.process
    if proc is None or proc.poll() is not None:
        instance.process = None
        return
    try:
        # Send SIGTERM to the process group (start_new_session=True)
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)
    except (ProcessLookupError, OSError):
        pass  # already dead
    instance.process = None


def _kwargs_to_args(kwargs: dict[str, str]) -> list[str]:
    """Convert kwargs dict to Docker command args (``--key value`` pairs)."""
    args: list[str] = []
    for key, value in sorted(kwargs.items()):
        args.extend([f"--{key}", value])
    return args


def _resolve_env_forward(env_forward: dict[str, str]) -> dict[str, str]:
    """Resolve ``env_forward`` mappings to concrete values from the host environment.

    Returns ``{container_var: resolved_value}`` for each host var that exists.
    Logs a warning for any host variable that is not set.
    """
    resolved: dict[str, str] = {}
    for container_var, host_var in sorted(env_forward.items()):
        value = os.environ.get(host_var)
        if value is None:
            logger.warning(
                "env_forward var not set on host — skipping",
                container_var=container_var,
                host_var=host_var,
            )
            continue
        resolved[container_var] = value
    return resolved


def _build_env_args(config: McpServerConfig) -> list[str]:
    """Build ``-e KEY=VALUE`` Docker flags from ``env`` and ``env_forward``.

    ``env_forward`` is a ``{container_var: host_var}`` dict (normalized from
    list or dict form by the Pydantic validator).
    """
    args: list[str] = []
    for key, value in sorted(config.env.items()):
        args.extend(["-e", f"{key}={value}"])
    for container_var, value in _resolve_env_forward(config.env_forward).items():
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
