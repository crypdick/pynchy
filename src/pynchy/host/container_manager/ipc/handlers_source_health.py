"""Read-only messaging-source health for container agents.

The host owns provider clients and the durable message store. Container agents
must use this projection instead of probing host environment variables or
opening provider applications just to determine whether a source works.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pynchy.host.container_manager.ipc.deps import (  # beartype resolves this runtime annotation.
    IpcDeps,
    MessagingSourceHealth,
    SourceHealthDeps,
)
from pynchy.host.container_manager.ipc.registry import register
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,
)

_CHANNEL_CONNECTION_TYPES = frozenset({"discord", "slack", "whatsapp"})


def _normalized(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _requested_sources(data: dict[str, Any]) -> tuple[str, ...] | None:
    value = data.get("sources")
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("sources must be a list of source names")
    return tuple(item.strip() for item in value if item.strip())


def _source_health(deps: IpcDeps) -> MessagingSourceHealth:
    if not isinstance(deps, SourceHealthDeps):
        raise TypeError("Messaging source health requires SourceHealthDeps")
    return deps.messaging_source_health()


def _matching_connection_names(requested: str, connections: dict[str, str]) -> tuple[str, ...]:
    wanted = _normalized(requested)
    return tuple(
        name
        for name, provider in connections.items()
        if wanted in {_normalized(name), _normalized(provider)}
    )


def _selected_sources(
    requested: tuple[str, ...] | None,
    connections: dict[str, str],
    source_health: MessagingSourceHealth,
) -> tuple[tuple[str, str | None, str | None], ...]:
    if requested is None:
        configured = tuple((name, name, None) for name in connections)
        configured_providers = set(connections.values())
        personal = tuple(
            (provider, None, provider)
            for provider in source_health.personal_providers()
            if provider not in configured_providers
        )
        return configured + personal

    selected: list[tuple[str, str | None, str | None]] = []
    seen: set[str] = set()
    for label in requested:
        matches = _matching_connection_names(label, connections)
        if not matches:
            selected.append((label, None, source_health.personal_provider_for(label)))
            continue
        for name in matches:
            if name not in seen:
                selected.append((label, name, None))
                seen.add(name)
    return tuple(selected)


def _channel_for(name: str, channels: tuple[Channel, ...]) -> Channel | None:
    return next((channel for channel in channels if channel.name == name), None)


def _owned_workspace_jids(channel: Channel, deps: IpcDeps) -> tuple[str, ...]:
    return tuple(jid for jid in deps.workspaces() if channel.owns_jid(jid))


def _connection_statuses(deps: IpcDeps) -> dict[str, bool]:
    """Read optional connection-runtime status without widening the core IPC contract."""
    status_getter = getattr(deps, "connection_statuses", None)
    if not callable(status_getter):
        return {}
    statuses = status_getter()
    if not isinstance(statuses, dict):
        return {}
    return {
        name: ready
        for name, ready in statuses.items()
        if isinstance(name, str) and isinstance(ready, bool)
    }


async def _channel_source(
    name: str,
    provider: str,
    channels: tuple[Channel, ...],
    deps: IpcDeps,
) -> dict[str, object]:
    channel = _channel_for(name, channels)
    if channel is None:
        return {
            "name": name,
            "provider": provider,
            "status": "unavailable",
            "ready": False,
            "latest_inbound_at": None,
            "reason": "The connection is configured but its Pynchy channel runtime is unavailable.",
        }

    latest = await _source_health(deps).get_latest_inbound_timestamp(
        _owned_workspace_jids(channel, deps)
    )
    ready = channel.is_connected()
    return {
        "name": name,
        "provider": provider,
        "status": "ready" if ready else "unavailable",
        "ready": ready,
        "latest_inbound_at": latest,
        "freshness_scope": "Pynchy-ingested inbound messages for registered workspaces",
        "reason": None if ready else "The Pynchy channel runtime reports disconnected.",
    }


def _connection_source(
    name: str,
    provider: str,
    connection_statuses: dict[str, bool],
) -> dict[str, object]:
    runtime_name = f"connection.{provider}.{name}"
    ready = connection_statuses.get(runtime_name, False)
    return {
        "name": name,
        "provider": provider,
        "status": "ready" if ready else "unavailable",
        "ready": ready,
        "latest_inbound_at": None,
        "freshness_scope": "Provider freshness is not projected by this Pynchy connection runtime",
        "reason": None if ready else "The configured Pynchy connection runtime is not ready.",
    }


async def _configured_source(
    name: str,
    connections: dict[str, str],
    channels: tuple[Channel, ...],
    connection_statuses: dict[str, bool],
    deps: IpcDeps,
) -> dict[str, object]:
    provider = connections[name]
    if provider in _CHANNEL_CONNECTION_TYPES:
        return await _channel_source(name, provider, channels, deps)
    return _connection_source(name, provider, connection_statuses)


def _not_established(requested: str) -> dict[str, object]:
    return {
        "name": requested,
        "provider": None,
        "status": "not_established",
        "ready": False,
        "latest_inbound_at": None,
        "reason": (
            "No configured Pynchy connection matches this source name or provider type. "
            "Pynchy cannot inspect its health or freshness."
        ),
    }


def _latest_inbound(sources: list[dict[str, object]]) -> dict[str, object] | None:
    candidates: list[tuple[datetime, dict[str, object]]] = []
    for source in sources:
        value = source.get("latest_inbound_at")
        if not isinstance(value, str):
            continue
        try:
            timestamp = datetime.fromisoformat(value)
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        candidates.append((timestamp, source))
    if not candidates:
        return None
    latest = max(candidates, key=lambda candidate: candidate[0])[1]
    return {
        "name": latest["name"],
        "provider": latest["provider"],
        "timestamp": latest["latest_inbound_at"],
    }


async def _handle_messaging_source_health(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registry callback signature.
    deps: IpcDeps,
) -> None:
    """Return provider readiness and persisted-ingress freshness without message bodies."""
    del is_admin
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        logger.warning(
            "Messaging source health request missing request_id",
            source_group=source_group,
        )
        return

    try:
        requested = _requested_sources(data)
    except ValueError as exc:
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": str(exc)},
        )
        return

    source_health = _source_health(deps)
    connections = source_health.configured_connections()
    channels = tuple(deps.channels())
    connection_statuses = _connection_statuses(deps)
    sources: list[dict[str, object]] = []
    for requested_name, configured_name, personal_provider in _selected_sources(
        requested, connections, source_health
    ):
        if configured_name is not None:
            sources.append(
                await _configured_source(
                    configured_name,
                    connections,
                    channels,
                    connection_statuses,
                    deps,
                )
            )
            continue
        if personal_provider is None:
            sources.append(_not_established(requested_name))
            continue
        sources.append(await source_health.project_personal_source(personal_provider))

    write_ipc_response(
        ipc_response_path(source_group, request_id),
        {
            "result": {
                "sources": sources,
                "latest_inbound": _latest_inbound(sources),
                "coverage": {
                    "scope": (
                        "configured Pynchy runtimes and configured host-local aggregate stores"
                    ),
                    "message_content_read": False,
                    "sender_identity_read": False,
                    "provider_read_state_changed": False,
                    "unknown_sources": "reported as not_established",
                },
            }
        },
    )


register("messaging_source_health", _handle_messaging_source_health)
