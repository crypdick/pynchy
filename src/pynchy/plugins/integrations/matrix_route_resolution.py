"""Resolve provider-neutral routes into typed Matrix runtime bindings."""

from __future__ import annotations

from collections.abc import (
    Callable,
    Iterable,
    Mapping,
)
from dataclasses import dataclass

from pynchy.integration_contracts import (
    MatrixActivation,
    MatrixConnection,
    MatrixEndpoint,
    MatrixOutbound,
)
from pynchy.workspace.api import (  # beartype resolves Matrix route annotations.
    CapabilityDecision,
    CapabilityRule,
    capability_pattern_matches,
    most_restrictive_capability_rule,
)


@dataclass(frozen=True, slots=True)
class MatrixEndpointRef:
    connection: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class MatrixRouteInput:
    """Configured route values required to resolve one Matrix binding."""

    name: str
    source: str
    workspace: str
    activation: MatrixActivation | None
    outbound: MatrixOutbound | None
    tools: tuple[str, ...] | None
    capabilities: dict[str, CapabilityDecision]


@dataclass(frozen=True, slots=True)
class MatrixWorkspacePolicy:
    """Resolved workspace policy consulted while resolving a Matrix route."""

    is_admin: bool
    tools: tuple[str, ...]
    capabilities: dict[str, CapabilityRule]


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
    capabilities: dict[str, CapabilityDecision]


def parse_matrix_endpoint_ref(value: str) -> MatrixEndpointRef | None:
    """Parse a full Matrix endpoint reference, including dotted endpoint names."""
    parts = value.split(".")
    if len(parts) < 5 or parts[:2] != ["connection", "matrix"] or parts[3] != "chat":
        return None
    connection, endpoint = parts[2], ".".join(parts[4:])
    if not connection or not endpoint:
        return None
    return MatrixEndpointRef(connection=connection, endpoint=endpoint)


def resolve_matrix_routes(
    routes: Iterable[MatrixRouteInput],
    connections: Mapping[str, MatrixConnection],
    workspace_policy: Callable[[str], MatrixWorkspacePolicy | None],
) -> tuple[ResolvedMatrixRoute, ...]:
    """Resolve exact policy and prove every route can only reduce privileges."""
    resolved_routes: list[ResolvedMatrixRoute] = []
    for route in routes:
        route_name = route.name
        source = parse_matrix_endpoint_ref(route.source)
        if source is None:
            continue
        connection = connections.get(source.connection)
        if connection is None:
            raise ValueError(f"Matrix route {route_name!r} references an unknown connection")
        endpoint = connection.chat[source.endpoint]
        workspace = workspace_policy(route.workspace)
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
            route_rule = CapabilityRule(decision=decision)
            inherited = most_restrictive_capability_rule(
                rule
                for pattern, rule in workspace.capabilities.items()
                if capability_pattern_matches(pattern, capability)
            )
            effective = most_restrictive_capability_rule(
                rule for rule in (inherited, route_rule) if rule is not None
            )
            if effective is not None and effective.decision != decision:
                weakened.append(capability)
        if weakened:
            raise ValueError(
                f"Matrix route {route_name!r} cannot weaken workspace permissions: "
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
