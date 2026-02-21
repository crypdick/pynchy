"""LiteLLM MCP sync — endpoint registration and team management.

Extracted from ``mcp_manager.py``.  These functions push MCP state to
LiteLLM via its HTTP API: registering/deregistering server endpoints,
managing per-workspace teams and virtual keys, and persisting the
team cache to disk.

All functions take explicit parameters rather than reaching into a
manager instance, keeping the boundary between orchestration (McpManager)
and I/O (this module) clean.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from pynchy.logger import logger

if TYPE_CHECKING:
    from pynchy.container_runner.gateway import LiteLLMGateway
    from pynchy.container_runner.mcp_manager import McpInstance, WorkspaceTeam


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def api_request(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    method: str,
    path: str,
    *,
    json_data: dict[str, Any] | None = None,
    log_event: str = "",
    **log_kwargs: Any,
) -> Any:
    """Make a LiteLLM API request with standard error handling.

    Returns parsed JSON on 2xx, ``None`` on failure.  Pass *log_event*
    to emit a warning on non-2xx or network error; leave empty to
    suppress failure logs (useful for best-effort deletes).
    """
    url = f"http://localhost:{gateway.port}{path}"
    headers = {"Authorization": f"Bearer {gateway.key}"}
    try:
        async with session.request(
            method,
            url,
            json=json_data,
            headers=headers,
        ) as resp:
            if resp.status in (200, 201):
                try:
                    return await resp.json()
                except (aiohttp.ContentTypeError, ValueError):
                    return True  # 2xx but no JSON body
            if log_event:
                body = await resp.text()
                logger.warning(log_event, status=resp.status, body=body[:500], **log_kwargs)
    except (aiohttp.ClientError, OSError) as exc:
        if log_event:
            logger.warning(log_event, error=str(exc), **log_kwargs)
    return None


# ---------------------------------------------------------------------------
# Endpoint sync
# ---------------------------------------------------------------------------


async def sync_mcp_endpoints(
    gateway: LiteLLMGateway,
    instances: dict[str, McpInstance],
) -> None:
    """Register/deregister MCP server endpoints in LiteLLM.

    Idempotent: deletes stale/duplicate registrations first, then creates
    missing ones.  Each desired instance ends up with exactly one entry.

    GOTCHA: LiteLLM has two similar-looking /mcp/ route families:
      - /mcp/*  -- the SSE/streamable-HTTP *transport* (for MCP clients)
      - /v1/mcp/server -- the REST *management* API (CRUD for server configs)
    Hitting /mcp/server/... returns a JSONRPC 406 "Not Acceptable" because
    it's the transport endpoint expecting SSE Accept headers.
    """
    async with aiohttp.ClientSession() as session:
        # Get currently registered servers.
        # NOTE: /v1/mcp/server returns a bare JSON array, not {"data": [...]}.
        # Collect ALL entries per name -- there may be duplicates from earlier bugs.
        existing: dict[str, list[dict[str, Any]]] = {}  # name -> [{server_id, url, ...}]
        data = await api_request(
            session,
            gateway,
            "GET",
            "/v1/mcp/server",
            log_event="Failed to list MCP servers from LiteLLM",
        )
        if isinstance(data, list):
            for srv in data:
                name = srv.get("server_name", "")
                existing.setdefault(name, []).append(srv)

        # ----------------------------------------------------------
        # For each desired instance, ensure exactly one registration
        # with the correct URL.  Delete extras and stale entries.
        # ----------------------------------------------------------
        # NOTE: LiteLLM field is "url", not "server_url".
        # NOTE: LiteLLM rejects server_name values containing hyphens.
        for iid, instance in instances.items():
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

            # Delete duplicates / stale-URL entries (best-effort, no log on failure)
            for sid in to_delete:
                if await api_request(session, gateway, "DELETE", f"/v1/mcp/server/{sid}"):
                    logger.info(
                        "Deleted duplicate MCP registration",
                        instance_id=iid,
                        server_id=sid,
                    )

            # Skip creation if we already have a matching entry
            if keep is not None:
                logger.debug("MCP endpoint already registered", instance_id=iid)
                continue

            # Register the instance.
            # allow_all_keys=True: per-workspace isolation is enforced by the
            # orchestrator (only workspaces that list this server in their
            # mcp_servers config get the gateway URL injected).  LiteLLM's
            # key->server ACL (allowed_mcp_servers on /key/generate) is not
            # reliably stored, so we use allow_all_keys instead.
            # LiteLLM accepts "sse" | "http" | "stdio"; map our config values.
            transport = instance.server_config.transport
            if transport == "streamable_http":
                transport = "http"

            payload: dict[str, Any] = {
                "server_name": iid,
                "url": desired_url,
                "transport": transport,
                "allow_all_keys": True,
            }

            # Add auth if configured
            if instance.server_config.auth_value_env:
                auth_value = os.environ.get(instance.server_config.auth_value_env, "")
                if auth_value:
                    payload["auth_value"] = auth_value

            result = await api_request(
                session,
                gateway,
                "POST",
                "/v1/mcp/server",
                json_data=payload,
                log_event="Failed to register MCP endpoint",
                instance_id=iid,
            )
            if result is not None:
                logger.info("Registered MCP endpoint", instance_id=iid)

        # ----------------------------------------------------------
        # Anything left in `existing` is not in instances --
        # delete ALL entries for those names (stale from old config).
        # ----------------------------------------------------------
        for name, entries in existing.items():
            for entry in entries:
                sid = entry.get("server_id", "")
                if await api_request(session, gateway, "DELETE", f"/v1/mcp/server/{sid}"):
                    logger.info("Deregistered stale MCP endpoint", name=name)


# ---------------------------------------------------------------------------
# Team sync
# ---------------------------------------------------------------------------


async def sync_teams(
    gateway: LiteLLMGateway,
    workspace_instances: dict[str, list[str]],
    workspace_teams: dict[str, WorkspaceTeam],
) -> None:
    """Create/update LiteLLM teams per workspace with MCP access control.

    Mutates *workspace_teams* in place: adds new entries for created teams,
    removes entries for stale workspaces.
    """
    from pynchy.container_runner.mcp_manager import WorkspaceTeam as _WT

    async with aiohttp.ClientSession() as session:
        for folder, instance_ids in workspace_instances.items():
            existing_team = workspace_teams.get(folder)

            # Create team if it doesn't exist
            if existing_team is None:
                team_id = await _create_team(session, gateway, folder, instance_ids)
                if team_id is None:
                    continue

                virtual_key = await _create_key(session, gateway, team_id, instance_ids)
                if virtual_key is None:
                    continue

                workspace_teams[folder] = _WT(
                    team_id=team_id,
                    virtual_key=virtual_key,
                )
                logger.info("Created MCP team", workspace=folder, team_id=team_id)
            else:
                # Update existing team's allowed servers
                await _update_team(session, gateway, existing_team.team_id, instance_ids)

    # Clean up teams for removed workspaces
    stale = set(workspace_teams) - set(workspace_instances)
    for folder in stale:
        team = workspace_teams.pop(folder)
        await delete_team(gateway, team.team_id)
        logger.info("Removed stale MCP team", workspace=folder)


async def _create_team(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    folder: str,
    instance_ids: list[str],
) -> str | None:
    """Create a LiteLLM team.  Returns team_id or None on failure."""
    data = await api_request(
        session,
        gateway,
        "POST",
        "/team/new",
        json_data={
            "team_alias": f"pynchy-mcp-{folder}",
            "metadata": {"pynchy_workspace": folder},
        },
        log_event="Failed to create team",
        workspace=folder,
    )
    return data.get("team_id") if isinstance(data, dict) else None


async def _create_key(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    team_id: str,
    instance_ids: list[str],
) -> str | None:
    """Generate a LiteLLM virtual key for a team.  Returns key or None."""
    data = await api_request(
        session,
        gateway,
        "POST",
        "/key/generate",
        json_data={
            "team_id": team_id,
            "allowed_mcp_servers": instance_ids,
        },
        log_event="Failed to generate key",
        team_id=team_id,
    )
    return data.get("key") if isinstance(data, dict) else None


async def _update_team(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    team_id: str,
    instance_ids: list[str],
) -> None:
    """Update a team's metadata."""
    await api_request(
        session,
        gateway,
        "POST",
        "/team/update",
        json_data={
            "team_id": team_id,
            "metadata": {"allowed_mcp_servers": instance_ids},
        },
        log_event="Failed to update team",
        team_id=team_id,
    )


async def delete_team(gateway: LiteLLMGateway, team_id: str) -> None:
    """Delete a LiteLLM team."""
    async with aiohttp.ClientSession() as session:
        await api_request(
            session,
            gateway,
            "POST",
            "/team/delete",
            json_data={"team_ids": [team_id]},
            log_event="Failed to delete team",
            team_id=team_id,
        )


# ---------------------------------------------------------------------------
# Team cache persistence
# ---------------------------------------------------------------------------


def load_teams_cache(
    cache_path: Path,
) -> dict[str, WorkspaceTeam]:
    """Load cached team_id -> virtual_key mapping from disk."""
    from pynchy.container_runner.mcp_manager import WorkspaceTeam as _WT

    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text())
        return {
            folder: _WT(
                team_id=team_data["team_id"],
                virtual_key=team_data["virtual_key"],
            )
            for folder, team_data in data.items()
        }
    except (json.JSONDecodeError, KeyError, TypeError):
        logger.warning("Failed to load MCP teams cache -- will recreate")
        return {}


def save_teams_cache(
    cache_path: Path,
    workspace_teams: dict[str, WorkspaceTeam],
) -> None:
    """Persist team_id -> virtual_key mapping to disk."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        folder: {"team_id": team.team_id, "virtual_key": team.virtual_key}
        for folder, team in workspace_teams.items()
    }
    cache_path.write_text(json.dumps(data, indent=2))
