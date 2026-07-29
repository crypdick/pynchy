"""Startup recovery for process-local routed workspace policy."""

from collections.abc import Iterable

from pynchy.conversation.api import conversation_id_from_folder
from pynchy.host.orchestrator.workspace_config import ensure_runtime_workspace_policy_owner
from pynchy.state.api import get_conversation
from pynchy.workspace.api import WorkspaceProfile


async def restore_routed_workspace_policy_owners(
    workspaces: Iterable[WorkspaceProfile],
) -> None:
    """Restore owner policy for open routed controls before startup recovery."""
    for workspace in workspaces:
        conversation_id = conversation_id_from_folder(workspace.folder)
        if conversation_id is None:
            continue
        conversation = await get_conversation(conversation_id)
        if conversation is not None and not conversation.control_closed:
            ensure_runtime_workspace_policy_owner(
                workspace.folder,
                conversation.workspace,
            )
