"""MCP server lifecycle manager — LiteLLM sync, Docker on-demand, team provisioning.

``config.toml`` is the single source of truth.  At boot, :meth:`McpManager.sync`
pushes MCP state to LiteLLM via its HTTP API.  Docker-based MCP containers start
on-demand when an agent first needs them and stop after an idle timeout.

Adding a new MCP is as simple as adding a ``[mcp_servers.<name>]`` section to
``config.toml`` — no policy files, no editing ``litellm_config.yaml``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp

from pynchy.container_runner._docker import (
    ensure_image,
    ensure_network,
    is_container_running,
    run_docker,
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
        # Docker: internal Docker network URL
        return f"http://{self.container_name}:{self.server_config.port}"


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
        self._load_teams_cache()

        # 3. Register MCP endpoints with LiteLLM
        await self._sync_mcp_endpoints()

        # 4. Sync teams and virtual keys
        await self._sync_teams()

        # 5. Persist team cache
        self._save_teams_cache()

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

        run_docker(
            "run", "-d",
            "--name", instance.container_name,
            "--network", _NETWORK_NAME,
            "--restart", "unless-stopped",
            *publish_args,
            instance.server_config.image or "",
            *cmd_args,
        )  # fmt: skip

        # Health-check via localhost (host-side), not the Docker-internal name
        health_url = f"http://localhost:{port}" if port else instance.endpoint_url
        try:
            await self._wait_mcp_healthy(instance.container_name, health_url)
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
    # Internal: LiteLLM HTTP API
    # ------------------------------------------------------------------

    def _api_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._gateway.key}"}

    def _api_url(self, path: str) -> str:
        return f"http://localhost:{self._gateway.port}{path}"

    async def _sync_mcp_endpoints(self) -> None:
        """Register/deregister MCP server endpoints in LiteLLM.

        Idempotent: deletes stale/duplicate registrations first, then creates
        missing ones.  Each desired instance ends up with exactly one entry.

        GOTCHA: LiteLLM has two similar-looking /mcp/ route families:
          - /mcp/*  — the SSE/streamable-HTTP *transport* (for MCP clients)
          - /v1/mcp/server — the REST *management* API (CRUD for server configs)
        Hitting /mcp/server/... returns a JSONRPC 406 "Not Acceptable" because
        it's the transport endpoint expecting SSE Accept headers.
        """
        async with aiohttp.ClientSession() as session:
            # Get currently registered servers.
            # NOTE: /v1/mcp/server returns a bare JSON array, not {"data": [...]}.
            # Collect ALL entries per name — there may be duplicates from earlier bugs.
            existing: dict[str, list[dict[str, Any]]] = {}  # name -> [{server_id, url, ...}]
            try:
                async with session.get(
                    self._api_url("/v1/mcp/server"),
                    headers=self._api_headers(),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        for srv in data:
                            name = srv.get("server_name", "")
                            existing.setdefault(name, []).append(srv)
            except (aiohttp.ClientError, OSError) as exc:
                logger.warning("Failed to list MCP servers from LiteLLM", error=str(exc))

            # ----------------------------------------------------------
            # For each desired instance, ensure exactly one registration
            # with the correct URL.  Delete extras and stale entries.
            # ----------------------------------------------------------
            # NOTE: LiteLLM field is "url", not "server_url".
            # NOTE: LiteLLM rejects server_name values containing hyphens.
            for iid, instance in self._instances.items():
                entries = existing.pop(iid, [])
                desired_url = instance.endpoint_url

                # Find an entry that already matches the desired URL
                keep: dict[str, Any] | None = None
                to_delete: list[str] = []
                for entry in entries:
                    if keep is None and entry.get("url") == desired_url:
                        keep = entry
                    else:
                        to_delete.append(entry.get("server_id", ""))

                # Delete duplicates / stale-URL entries
                for sid in to_delete:
                    try:
                        async with session.delete(
                            self._api_url(f"/v1/mcp/server/{sid}"),
                            headers=self._api_headers(),
                        ) as resp:
                            logger.info(
                                "Deleted duplicate MCP registration",
                                instance_id=iid,
                                server_id=sid,
                            )
                    except (aiohttp.ClientError, OSError):
                        pass

                # Skip creation if we already have a matching entry
                if keep is not None:
                    logger.debug("MCP endpoint already registered", instance_id=iid)
                    continue

                # Register the instance.
                # allow_all_keys=True: per-workspace isolation is enforced by the
                # orchestrator (only workspaces that list this server in their
                # mcp_servers config get the gateway URL injected).  LiteLLM's
                # key→server ACL (allowed_mcp_servers on /key/generate) is not
                # reliably stored, so we use allow_all_keys instead.
                payload: dict[str, Any] = {
                    "server_name": iid,
                    "url": desired_url,
                    "transport": instance.server_config.transport,
                    "allow_all_keys": True,
                }

                # Add auth if configured
                if instance.server_config.auth_value_env:
                    import os

                    auth_value = os.environ.get(instance.server_config.auth_value_env, "")
                    if auth_value:
                        payload["auth_value"] = auth_value

                try:
                    async with session.post(
                        self._api_url("/v1/mcp/server"),
                        json=payload,
                        headers=self._api_headers(),
                    ) as resp:
                        if resp.status in (200, 201):
                            logger.info("Registered MCP endpoint", instance_id=iid)
                        else:
                            body = await resp.text()
                            logger.warning(
                                "Failed to register MCP endpoint",
                                instance_id=iid,
                                status=resp.status,
                                body=body[:500],
                            )
                except (aiohttp.ClientError, OSError) as exc:
                    logger.warning(
                        "Failed to register MCP endpoint",
                        instance_id=iid,
                        error=str(exc),
                    )

            # ----------------------------------------------------------
            # Anything left in `existing` is not in self._instances —
            # delete ALL entries for those names (stale from old config).
            # ----------------------------------------------------------
            for name, entries in existing.items():
                for entry in entries:
                    sid = entry.get("server_id", "")
                    try:
                        async with session.delete(
                            self._api_url(f"/v1/mcp/server/{sid}"),
                            headers=self._api_headers(),
                        ) as resp:
                            logger.info("Deregistered stale MCP endpoint", name=name)
                    except (aiohttp.ClientError, OSError):
                        pass

    async def _sync_teams(self) -> None:
        """Create/update LiteLLM teams per workspace with MCP access control."""
        async with aiohttp.ClientSession() as session:
            for folder, instance_ids in self._workspace_instances.items():
                existing_team = self._workspace_teams.get(folder)

                # Create team if it doesn't exist
                if existing_team is None:
                    team_id = await self._create_team(session, folder, instance_ids)
                    if team_id is None:
                        continue

                    virtual_key = await self._create_key(session, team_id, instance_ids)
                    if virtual_key is None:
                        continue

                    self._workspace_teams[folder] = WorkspaceTeam(
                        team_id=team_id,
                        virtual_key=virtual_key,
                    )
                    logger.info("Created MCP team", workspace=folder, team_id=team_id)
                else:
                    # Update existing team's allowed servers
                    await self._update_team(session, existing_team.team_id, instance_ids)

        # Clean up teams for removed workspaces
        stale = set(self._workspace_teams) - set(self._workspace_instances)
        for folder in stale:
            team = self._workspace_teams.pop(folder)
            await self._delete_team(team.team_id)
            logger.info("Removed stale MCP team", workspace=folder)

    async def _create_team(
        self,
        session: aiohttp.ClientSession,
        folder: str,
        instance_ids: list[str],
    ) -> str | None:
        """Create a LiteLLM team. Returns team_id or None on failure."""
        payload = {
            "team_alias": f"pynchy-mcp-{folder}",
            "metadata": {"pynchy_workspace": folder},
        }
        try:
            async with session.post(
                self._api_url("/team/new"),
                json=payload,
                headers=self._api_headers(),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    return data.get("team_id")
                body = await resp.text()
                logger.warning("Failed to create team", workspace=folder, body=body[:500])
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning("Failed to create team", workspace=folder, error=str(exc))
        return None

    async def _create_key(
        self,
        session: aiohttp.ClientSession,
        team_id: str,
        instance_ids: list[str],
    ) -> str | None:
        """Generate a LiteLLM virtual key for a team. Returns key or None."""
        payload = {
            "team_id": team_id,
            "allowed_mcp_servers": instance_ids,
        }
        try:
            async with session.post(
                self._api_url("/key/generate"),
                json=payload,
                headers=self._api_headers(),
            ) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    return data.get("key")
                body = await resp.text()
                logger.warning("Failed to generate key", team_id=team_id, body=body[:500])
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning("Failed to generate key", team_id=team_id, error=str(exc))
        return None

    async def _update_team(
        self,
        session: aiohttp.ClientSession,
        team_id: str,
        instance_ids: list[str],
    ) -> None:
        """Update a team's metadata."""
        payload = {
            "team_id": team_id,
            "metadata": {"allowed_mcp_servers": instance_ids},
        }
        try:
            async with session.post(
                self._api_url("/team/update"),
                json=payload,
                headers=self._api_headers(),
            ) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.warning("Failed to update team", team_id=team_id, body=body[:500])
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning("Failed to update team", team_id=team_id, error=str(exc))

    async def _delete_team(self, team_id: str) -> None:
        """Delete a LiteLLM team."""
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self._api_url("/team/delete"),
                    json={"team_ids": [team_id]},
                    headers=self._api_headers(),
                ) as resp,
            ):
                if resp.status not in (200, 201):
                    logger.warning("Failed to delete team", team_id=team_id)
        except (aiohttp.ClientError, OSError):
            pass

    # ------------------------------------------------------------------
    # Internal: Docker health check
    # ------------------------------------------------------------------

    @staticmethod
    async def _wait_mcp_healthy(
        container_name: str,
        endpoint_url: str,
        timeout: float = 60,
        poll_interval: float = 1.0,
    ) -> None:
        """Wait for an MCP container to respond to HTTP requests."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5),
        ) as session:
            while loop.time() < deadline:
                try:
                    async with session.get(endpoint_url) as resp:
                        # MCP servers might return various success codes
                        if resp.status < 500:
                            return
                except (aiohttp.ClientError, OSError):
                    pass

                if not is_container_running(container_name):
                    logs = run_docker("logs", "--tail", "30", container_name, check=False)
                    logger.error(
                        "MCP container exited",
                        container=container_name,
                        logs=logs.stdout[-2000:],
                    )
                    msg = f"MCP container {container_name} failed to start"
                    raise RuntimeError(msg)

                await asyncio.sleep(poll_interval)

        msg = f"MCP container {container_name} not healthy within {timeout}s"
        raise TimeoutError(msg)

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

    # ------------------------------------------------------------------
    # Internal: teams cache persistence
    # ------------------------------------------------------------------

    def _load_teams_cache(self) -> None:
        """Load cached team_id → virtual_key mapping from disk."""
        if not self._teams_cache_path.exists():
            return
        try:
            data = json.loads(self._teams_cache_path.read_text())
            for folder, team_data in data.items():
                self._workspace_teams[folder] = WorkspaceTeam(
                    team_id=team_data["team_id"],
                    virtual_key=team_data["virtual_key"],
                )
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Failed to load MCP teams cache — will recreate")

    def _save_teams_cache(self) -> None:
        """Persist team_id → virtual_key mapping to disk."""
        self._teams_cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            folder: {"team_id": team.team_id, "virtual_key": team.virtual_key}
            for folder, team in self._workspace_teams.items()
        }
        self._teams_cache_path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _kwargs_to_args(kwargs: dict[str, str]) -> list[str]:
    """Convert kwargs dict to Docker command args (``--key value`` pairs)."""
    args: list[str] = []
    for key, value in sorted(kwargs.items()):
        args.extend([f"--{key}", value])
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
