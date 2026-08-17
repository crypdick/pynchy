"""Host projection of active Linear issues into silent forum controls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pynchy.conversation.api import (
    Conversation,  # noqa: TC001 - beartype resolves this runtime annotation.
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    ensure_conversation_workspace,
)
from pynchy.host.orchestrator.threads import (
    ensure_forum_guidelines_linked,
    ensure_thread_link_pinned,
)
from pynchy.host.orchestrator.workspace_config import ensure_runtime_workspace_policy_owner
from pynchy.identifiers import ChatJid, GroupFolder
from pynchy.state.api import (
    apply_conversation_control_state,
    get_conversation_control_binding,
)

if TYPE_CHECKING:
    from pynchy.host.orchestrator.app import PynchyApp
else:
    PynchyApp = Any


@runtime_checkable
class LinearIssueControlLike(Protocol):
    """Integration-neutral issue control fields consumed by the host."""

    @property
    def issue_id(self) -> str: ...

    @property
    def workspace(self) -> str: ...

    @property
    def parent_jid(self) -> str: ...

    @property
    def account_name(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def updated_at(self) -> str: ...


async def ensure_forum_guidelines(app: PynchyApp, boards: dict[str, Any]) -> None:
    """Link each Linear board's Discord forum to its managed project."""
    for workspace, board in boards.items():
        project_url = board.project.get("url")
        root = next(
            (profile for profile in app.workspaces.values() if profile.folder == workspace),
            None,
        )
        if root is not None and isinstance(project_url, str) and project_url:
            await ensure_forum_guidelines_linked(app.channels, root.jid, project_url)


async def ensure_issue_control(
    app: PynchyApp,
    control: LinearIssueControlLike,
    conversation: Conversation,
) -> None:
    """Ensure one active Linear issue has a forum post without waking its agent."""
    await apply_conversation_control_state(
        conversation.id,
        closed=False,
        control_state_revision=control.updated_at,
    )
    binding = await get_conversation_control_binding(conversation.id)
    if binding is not None and binding.parent_jid == control.parent_jid and not binding.closed:
        profile = app.workspaces.get(binding.thread_jid)
        if profile is not None:
            ensure_runtime_workspace_policy_owner(profile.folder, conversation.workspace)
            await ensure_thread_link_pinned(app.channels, binding.thread_jid, control.url)
            return
    parent = app.workspaces.get(control.parent_jid)
    if parent is None:
        raise ValueError("Linear issue control parent workspace is not registered")
    ensured = await ensure_conversation_workspace(
        ConversationWorkspaceContext(
            channels=lambda: app.channels,
            workspaces=lambda: app.workspaces,
            register_workspace=app.register_workspace,
            unregister_workspace=app.unregister_workspace,
            rebind_workspace=app.rebind_workspace,
            bind_session=app.bind_routed_session,
        ),
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder(parent.folder),
            parent_jid=ChatJid(control.parent_jid),
            title=control.title,
            owner_workspace=conversation.workspace,
            kind="issue",
        ),
    )
    ensure_runtime_workspace_policy_owner(ensured.profile.folder, conversation.workspace)
    await ensure_thread_link_pinned(app.channels, ensured.control.binding.thread_jid, control.url)
