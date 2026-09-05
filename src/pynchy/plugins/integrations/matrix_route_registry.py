"""Host-only bindings from active agent workspaces to exact Matrix routes."""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.conversation.api import (  # noqa: TC001 - beartype resolves binding annotations.
    ConversationId,
)
from pynchy.identifiers import (
    ChatJid,  # noqa: TC001 - beartype resolves binding annotations.
)
from pynchy.plugins.integrations.matrix_gateway_client import (  # noqa: TC001 - beartype resolves binding annotations.
    MatrixPortalAssertion,
)
from pynchy.plugins.integrations.matrix_route_resolution import (  # noqa: TC001 - beartype resolves binding annotations.
    ResolvedMatrixRoute,
)


@dataclass(frozen=True, slots=True)
class ActiveMatrixRoute:
    """Current host-resolved provider destination for one conversation workspace."""

    workspace_folder: str
    conversation_id: ConversationId
    control_thread_jid: ChatJid
    route: ResolvedMatrixRoute
    portal: MatrixPortalAssertion


_active_routes: dict[str, ActiveMatrixRoute] = {}


def bind_active_matrix_route(binding: ActiveMatrixRoute) -> None:
    """Bind one workspace to its successfully reconciled route assertion."""
    _active_routes[binding.workspace_folder] = binding


def get_active_matrix_route(workspace_folder: str) -> ActiveMatrixRoute | None:
    """Return the exact route available to one active conversation workspace."""
    return _active_routes.get(workspace_folder)


def clear_active_matrix_routes() -> None:  # noqa: V103
    """Clear process-local assertions during shutdown and hermetic tests."""
    _active_routes.clear()


def clear_active_matrix_connection(connection_name: str) -> tuple[str, ...]:
    """Clear one named connection's bindings and return their workspace folders."""
    folders = tuple(
        folder
        for folder, binding in _active_routes.items()
        if binding.route.connection_name == connection_name
    )
    for folder in folders:
        _active_routes.pop(folder, None)
    return folders
