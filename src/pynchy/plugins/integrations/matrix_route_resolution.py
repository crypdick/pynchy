"""Resolve provider-neutral routes into typed Matrix runtime bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pynchy.config.api import (  # noqa: TC001, RUF100 - beartype resolves route settings.
    Settings,
)
from pynchy.types import (
    MatrixActivation,
    MatrixConnection,
    MatrixEndpoint,
    MatrixOutbound,
    capability_pattern_matches,
    is_matrix_connection,
    most_restrictive_capability_rule,
)


@dataclass(frozen=True, slots=True)
class MatrixEndpointRef:
    connection: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class ResolvedMatrixRoute:
    name: str
    connection_name: str
    connection: MatrixConnection
    endpoint_name: str
    endpoint: MatrixEndpoint
    control_title: str
    workspace: str
    activation: MatrixActivation
    outbound: MatrixOutbound
    tools: tuple[str, ...] | None
    capabilities: dict[str, Literal["deny", "needs_human"]]


def parse_matrix_endpoint_ref(value: str) -> MatrixEndpointRef | None:
    """Parse a full Matrix endpoint reference, including dotted endpoint names."""
    parts = value.split(".")
    if len(parts) < 5 or parts[:2] != ["connection", "matrix"] or parts[3] != "chat":
        return None
    connection, endpoint = parts[2], ".".join(parts[4:])
    if not connection or not endpoint:
        return None
    return MatrixEndpointRef(connection=connection, endpoint=endpoint)


def resolve_matrix_routes(settings: Settings) -> tuple[ResolvedMatrixRoute, ...]:
    """Resolve exact policy and prove every route can only reduce privileges."""
    resolved_routes: list[ResolvedMatrixRoute] = []
    for route_name, route in settings.routes.items():
        source = parse_matrix_endpoint_ref(route.source)
        if source is None:
            continue
        raw_connection = settings.connections[source.connection]
        if not is_matrix_connection(raw_connection):
            raise TypeError(f"Matrix route {route_name!r} resolved a non-Matrix connection")
        connection = raw_connection
        endpoint = connection.chat[source.endpoint]
        workspace = settings.resolved_workspace_config(route.workspace)
        if workspace is None:
            raise ValueError(f"Matrix route {route_name!r} references an unknown workspace")
        activation = route.activation or connection.route_defaults.activation
        requested_outbound = route.outbound or connection.route_defaults.outbound
        outbound: MatrixOutbound = (
            "read_only"
            if "read_only" in {connection.route_defaults.outbound, requested_outbound}
            else "approval_required"
        )
        if activation == "on_event" and workspace.is_admin:
            raise ValueError(
                f"Matrix route {route_name!r} cannot deliver untrusted events to an admin workspace"
            )
        if route.tools is not None and not set(route.tools).issubset(workspace.tools):
            raise ValueError(
                f"Matrix route {route_name!r} tools must be a restriction of workspace tools"
            )
        weakened = []
        for capability, decision in route.capabilities.items():
            inherited = most_restrictive_capability_rule(
                rule
                for pattern, rule in workspace.capabilities.items()
                if capability_pattern_matches(pattern, capability)
            )
            if inherited is not None and inherited.decision == "deny" and decision != "deny":
                weakened.append(capability)
        if weakened:
            raise ValueError(
                f"Matrix route {route_name!r} capabilities cannot weaken workspace denial: "
                + ", ".join(sorted(weakened))
            )
        resolved_routes.append(
            ResolvedMatrixRoute(
                name=route_name,
                connection_name=source.connection,
                connection=connection,
                endpoint_name=source.endpoint,
                endpoint=endpoint,
                control_title=endpoint.title or source.endpoint.replace("-", " ").title(),
                workspace=str(route.workspace),
                activation=activation,
                outbound=outbound,
                tools=(
                    tuple(str(tool) for tool in route.tools) if route.tools is not None else None
                ),
                capabilities=dict(route.capabilities),
            )
        )
    return tuple(resolved_routes)
