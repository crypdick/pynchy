"""Reconcile replaceable Discord controls for durable routed conversations."""

from __future__ import annotations

import sqlite3
from collections.abc import (  # noqa: TC003, RUF100 - beartype resolves context callbacks.
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.config.workspace_names import parent_workspace_name
from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.threads import ensure_thread
from pynchy.state import (
    get_conversation,
    get_conversation_control_by_thread,
    get_workspace_profile,
    set_conversation_control_binding,
)
from pynchy.types import Channel, ChatJid, GroupFolder, SessionId, WorkspaceProfile


@dataclass(frozen=True, slots=True)
class ConversationControlRequest:
    """Current human-facing placement and title for one conversation."""

    conversation_id: ConversationId
    parent_workspace: GroupFolder
    parent_jid: ChatJid
    title: str

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Conversation control title must not be empty")


@dataclass(frozen=True, slots=True)
class EnsuredConversationControl:
    """Result of reconciling one conversation's replaceable control thread."""

    binding: ConversationControlBinding
    created: bool


@dataclass(frozen=True, slots=True)
class ConversationWorkspaceContext:
    """Host callbacks needed to place one routed conversation workspace."""

    channels: Callable[[], list[Channel]]
    workspaces: Callable[[], dict[str, WorkspaceProfile]]
    register_workspace: Callable[[WorkspaceProfile], Awaitable[None]]
    unregister_workspace: Callable[[str], Awaitable[None]]
    bind_session: Callable[[str, SessionId], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class EnsuredConversationWorkspace:
    """Stable runtime workspace and replaceable human-facing control."""

    profile: WorkspaceProfile
    control: EnsuredConversationControl


def _workspace_shape(profile: WorkspaceProfile) -> tuple[object, ...]:
    return (
        profile.name,
        profile.folder,
        profile.trigger,
        profile.container_config,
        profile.security,
        profile.is_admin,
    )


async def ensure_conversation_control(
    channels: list[Channel],
    request: ConversationControlRequest,
) -> EnsuredConversationControl:
    """Ensure a readable Discord thread and persist it as current presentation.

    Lookup runs on every reconciliation. A missing or archived binding gets a
    usable thread without making its thread JID the conversation identity.
    """
    conversation = await get_conversation(request.conversation_id)
    if conversation is None:
        raise ValueError(f"Unknown conversation: {request.conversation_id}")
    if not request.parent_jid.startswith("discord:"):
        raise ValueError("Conversation control parent must belong to Discord")

    parent = await get_workspace_profile(request.parent_jid)
    if (
        parent is None
        or parent.folder != request.parent_workspace
        or parent_workspace_name(parent.folder) is not None
    ):
        raise ValueError("Conversation control parent must be a registered workspace root")

    index = 1
    while True:
        title = request.title if index == 1 else f"{request.title} ({index})"
        ensured = await ensure_thread(
            channels,
            request.parent_jid,
            title,
        )
        if ensured.jid is None:
            raise RuntimeError("Ensured conversation control returned no chat JID")
        thread_jid = ChatJid(ensured.jid)
        owner = await get_conversation_control_by_thread(thread_jid)
        if owner is not None and owner.conversation_id != request.conversation_id:
            index += 1
            continue

        binding = ConversationControlBinding(
            conversation_id=request.conversation_id,
            surface=ControlSurface.DISCORD,
            parent_workspace=request.parent_workspace,
            parent_jid=request.parent_jid,
            thread_jid=thread_jid,
            title=title,
            updated_at=datetime.now(UTC).isoformat(),
        )
        try:
            await set_conversation_control_binding(binding)
        except sqlite3.IntegrityError:
            # Another connection runtime may have claimed this readable name
            # between lookup and persistence. Retry with the next readable
            # suffix instead of exposing an internal conversation ID.
            owner = await get_conversation_control_by_thread(thread_jid)
            if owner is None or owner.conversation_id == request.conversation_id:
                raise
            index += 1
            continue
        return EnsuredConversationControl(binding=binding, created=ensured.created)


async def ensure_conversation_workspace(
    context: ConversationWorkspaceContext,
    request: ConversationControlRequest,
) -> EnsuredConversationWorkspace:
    """Place one stable conversation runtime behind its current Discord thread."""
    parent = next(
        (
            profile
            for profile in context.workspaces().values()
            if profile.folder == request.parent_workspace
        ),
        None,
    )
    if parent is None or parent.jid != request.parent_jid:
        raise ValueError("Conversation control parent workspace is not registered")

    control = await ensure_conversation_control(context.channels(), request)
    conversation = await get_conversation(request.conversation_id)
    if conversation is None:
        raise RuntimeError("Conversation disappeared while placing its workspace")
    folder = routed_conversation_folder(request.parent_workspace, conversation.id)

    for jid, existing in list(context.workspaces().items()):
        if existing.folder == folder and jid != control.binding.thread_jid:
            await context.unregister_workspace(jid)

    profile = WorkspaceProfile(
        jid=control.binding.thread_jid,
        name=f"{parent.name}/{control.binding.title}",
        folder=folder,
        trigger=parent.trigger,
        container_config=parent.container_config,
        security=parent.security,
        is_admin=False,
        added_at=datetime.now(UTC).isoformat(),
    )
    current = context.workspaces().get(profile.jid)
    if current is None or _workspace_shape(current) != _workspace_shape(profile):
        await context.register_workspace(profile)
    if conversation.session_id is not None:
        await context.bind_session(folder, conversation.session_id)
    return EnsuredConversationWorkspace(profile=profile, control=control)
