"""Synchronize MCP endpoints and workspace teams with LiteLLM."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import aiohttp

from pynchy.host.container_manager.gateway_litellm import (
    LiteLLMGateway,
)
from pynchy.host.container_manager.mcp.resolution import McpInstance, WorkspaceTeam
from pynchy.logger import logger

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


async def api_request(  # noqa: PLR0913 - stable request helper; call sites already pass these transport knobs explicitly.
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    method: str,
    path: str,
    *,
    json_data: dict[str, Any] | None = None,
    log_event: str = "",
    **log_kwargs: object,
) -> object | None:
    """Make a LiteLLM API request with standard error handling.

    Returns parsed JSON on 2xx, ``None`` on failure.  Pass *log_event*
    to emit a warning on non-2xx or network error; leave empty to
    suppress failure logs (useful for best-effort deletes).
    """
    url = f"http://localhost:{gateway.port}{path}"
    headers = {"Authorization": f"Bearer {gateway.key}"}
    try:
        async with session.request(method, url, json=json_data, headers=headers) as response:
            return await _api_response_data(response, log_event, log_kwargs)
    except (aiohttp.ClientError, OSError) as exc:
        if log_event:
            logger.warning(log_event, error=str(exc), **log_kwargs)
    return None


async def _api_response_data(
    response: aiohttp.ClientResponse,
    log_event: str,
    log_kwargs: dict[str, object],
) -> object | None:
    if 200 <= response.status < 300:
        try:
            return cast("object", await response.json())
        except (aiohttp.ContentTypeError, ValueError):
            return True  # 2xx but no JSON body
    if log_event:
        body = await response.text()
        logger.warning(log_event, status=response.status, body=body[:500], **log_kwargs)
    return None


# ---------------------------------------------------------------------------
# Endpoint sync
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _EndpointRegistration:
    server_id: str
    url: str


def _registered_endpoints(data: object) -> dict[str, list[_EndpointRegistration]]:
    endpoints: dict[str, list[_EndpointRegistration]] = {}
    if not isinstance(data, list):
        return endpoints

    for raw_entry in data:
        if not isinstance(raw_entry, dict):
            continue
        name = raw_entry.get("server_name")
        if not isinstance(name, str) or not name:
            continue
        endpoints.setdefault(name, []).append(
            _EndpointRegistration(
                server_id=str(raw_entry.get("server_id", "")),
                url=str(raw_entry.get("url", "")),
            )
        )
    return endpoints


def _sanitized_instance_id(instance_id: str) -> str:
    return instance_id.replace(".", "_").replace("-", "_")


def _registration_partition(
    registrations: list[_EndpointRegistration],
    desired_url: str,
) -> tuple[_EndpointRegistration | None, list[str]]:
    keep: _EndpointRegistration | None = None
    to_delete: list[str] = []
    for registration in registrations:
        if keep is None and registration.url == desired_url:
            keep = registration
            continue
        to_delete.append(registration.server_id)
    return keep, to_delete


async def _delete_registrations(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    *,
    server_ids: list[str],
    log_event: str,
    **log_kwargs: str,
) -> None:
    for server_id in server_ids:
        if not server_id:
            continue
        if await api_request(
            session,
            gateway,
            "DELETE",
            f"/v1/mcp/server/{server_id}",
        ):
            logger.info(log_event, server_id=server_id, **log_kwargs)


def _registration_transport(instance: McpInstance) -> str:
    transport = instance.server_config.transport
    return "http" if transport == "streamable_http" else transport


def _registration_payload(instance_id: str, instance: McpInstance) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "server_name": _sanitized_instance_id(instance_id),
        "url": instance.endpoint_url,
        "transport": _registration_transport(instance),
        "allow_all_keys": True,
    }

    auth_env = instance.server_config.auth_value_env
    if auth_env is None:
        return payload

    auth_value = os.environ.get(auth_env, "")
    if auth_value:
        payload["auth_value"] = auth_value
    return payload


async def _sync_instance_endpoint(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    *,
    instance_id: str,
    instance: McpInstance,
    existing: dict[str, list[_EndpointRegistration]],
) -> None:
    sanitized_id = _sanitized_instance_id(instance_id)
    registrations = existing.pop(sanitized_id, [])
    keep, to_delete = _registration_partition(registrations, instance.endpoint_url)
    await _delete_registrations(
        session,
        gateway,
        server_ids=to_delete,
        log_event="Deleted duplicate MCP registration",
        instance_id=instance_id,
    )

    if keep is not None:
        logger.debug("MCP endpoint already registered", instance_id=instance_id)
        return

    result = await api_request(
        session,
        gateway,
        "POST",
        "/v1/mcp/server",
        json_data=_registration_payload(instance_id, instance),
        log_event="Failed to register MCP endpoint",
        instance_id=instance_id,
    )
    if result is not None:
        logger.info("Registered MCP endpoint", instance_id=instance_id)


async def _delete_stale_endpoints(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    existing: dict[str, list[_EndpointRegistration]],
) -> None:
    for name, registrations in existing.items():
        await _delete_registrations(
            session,
            gateway,
            server_ids=[registration.server_id for registration in registrations],
            log_event="Deregistered stale MCP endpoint",
            name=name,
        )


async def _verify_empty_inventory(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    instances: dict[str, McpInstance],
) -> None:
    """Prune registrations omitted by LiteLLM's first post-startup inventory."""
    existing = _registered_endpoints(
        await api_request(
            session,
            gateway,
            "GET",
            "/v1/mcp/server",
            log_event="Failed to verify MCP servers from LiteLLM",
        )
    )
    for instance_id, instance in instances.items():
        registrations = existing.pop(_sanitized_instance_id(instance_id), [])
        _keep, to_delete = _registration_partition(registrations, instance.endpoint_url)
        await _delete_registrations(
            session,
            gateway,
            server_ids=to_delete,
            log_event="Deleted duplicate MCP registration",
            instance_id=instance_id,
        )
    await _delete_stale_endpoints(session, gateway, existing)


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
        existing = _registered_endpoints(
            await api_request(
                session,
                gateway,
                "GET",
                "/v1/mcp/server",
                log_event="Failed to list MCP servers from LiteLLM",
            )
        )
        verify_empty_inventory = not existing and bool(instances)

        for instance_id, instance in instances.items():
            await _sync_instance_endpoint(
                session,
                gateway,
                instance_id=instance_id,
                instance=instance,
                existing=existing,
            )

        await _delete_stale_endpoints(session, gateway, existing)
        if verify_empty_inventory:
            await _verify_empty_inventory(session, gateway, instances)


# ---------------------------------------------------------------------------
# Team sync
# ---------------------------------------------------------------------------


async def sync_teams(
    gateway: LiteLLMGateway,
    workspace_instances: dict[str, list[str]],
    workspace_teams: dict[str, WorkspaceTeam],
) -> None:
    """Create/update LiteLLM teams per workspace with MCP access control.

    Mutates *workspace_teams* in place: adds entries for created teams,
    removes entries for stale workspaces.
    """
    async with aiohttp.ClientSession() as session:
        for folder, instance_ids in workspace_instances.items():
            existing_team = workspace_teams.get(folder)

            # Create team if it doesn't exist
            if existing_team is None:
                team_id = await _create_team(session, gateway, folder)
                if team_id is None:
                    continue

                virtual_key = await _create_key(session, gateway, team_id, instance_ids)
                if virtual_key is None:
                    continue

                workspace_teams[folder] = WorkspaceTeam(
                    team_id=team_id,
                    virtual_key=virtual_key,
                )
                logger.info("Created MCP team", workspace=folder, team_id=team_id)
            else:
                # Update existing team's allowed servers
                await _update_team(session, gateway, existing_team.team_id, instance_ids)

    # Delete teams for stale workspaces
    stale = set(workspace_teams) - set(workspace_instances)
    for folder in stale:
        team = workspace_teams.pop(folder)
        await delete_team(gateway, team.team_id)
        logger.info("Removed stale MCP team", workspace=folder)


async def _create_team(
    session: aiohttp.ClientSession,
    gateway: LiteLLMGateway,
    folder: str,
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
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return {
            folder: WorkspaceTeam(
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
    cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
