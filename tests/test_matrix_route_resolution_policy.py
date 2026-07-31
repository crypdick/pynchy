"""Policy-preserving Matrix route resolution tests."""

from pynchy.config.api import MatrixConnectionConfig, MatrixEndpointConfig
from pynchy.plugins.integrations.matrix_route_resolution import (
    MatrixRouteInput,
    MatrixWorkspacePolicy,
    resolve_matrix_routes,
)
from pynchy.workspace.api import CapabilityRule


def test_route_policy_allows_explicit_denial_in_a_denied_parent_workspace() -> None:
    capability = "chat.matrix.route.read"
    connection = MatrixConnectionConfig(
        expected_user_id="@me:matrix.example.com",
        chat={"family": MatrixEndpointConfig(room_id="!family:matrix.example.com")},
    )
    route = MatrixRouteInput(
        name="family",
        source="connection.matrix.personal-chats.chat.family",
        workspace="support",
        activation=None,
        outbound=None,
        tools=None,
        capabilities={capability: "deny"},
    )
    policy = MatrixWorkspacePolicy(
        is_admin=False,
        tools=(),
        capabilities={capability: CapabilityRule(decision="deny")},
    )

    [resolved] = resolve_matrix_routes(
        (route,), {"personal-chats": connection}, lambda _workspace: policy
    )

    assert resolved.capabilities == {capability: "deny"}
