"""Reconcile replaceable Discord controls for durable routed conversations."""

from __future__ import annotations

import sqlite3
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from pynchy.conversation.api import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
    ConversationSubject,
    conversation_runtime_lock,
    parent_workspace_name,
    routed_conversation_folder,
)
from pynchy.host.orchestrator.threads import ThreadKind, ensure_thread, set_thread_closed
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.plugins.api import (
    Channel,
)
from pynchy.state.api import (
    ConversationControlWorkspaceChangedError,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_control_by_thread,
    get_conversation_for_subject,
    get_workspace_profile,
    set_conversation_control_binding,
)
from pynchy.workspace.api import WorkspaceProfile

_DISCORD_THREAD_TITLE_MAX_LENGTH = 100


@dataclass(frozen=True, slots=True)
class ConversationControlRequest:
    """Current human-facing placement and title for one conversation."""

    conversation_id: ConversationId
    parent_workspace: GroupFolder
    parent_jid: ChatJid
    title: str
    owner_workspace: GroupFolder | None = None
    kind: ThreadKind = "topic"

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("Conversation control title must not be empty")


class ConversationControlClosedError(RuntimeError):
    """A terminal conversation has no control thread to reopen."""


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
    rebind_workspace: Callable[[WorkspaceProfile], Awaitable[None]] | None = None


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

    current_binding = await get_conversation_control_binding(request.conversation_id)
    if conversation.control_closed:
        if current_binding is None:
            raise ConversationControlClosedError(
                f"Conversation control is closed: {request.conversation_id}"
            )
        # Discord's name lookup reopens archived threads, so a terminal control
        # returns its durable binding without attempting lookup or creation.
        return EnsuredConversationControl(
            binding=replace(current_binding, closed=True),
            created=False,
        )

    base_title = request.title.strip()
    index = 1
    while True:
        suffix = "" if index == 1 else f" ({index})"
        title = f"{base_title[: _DISCORD_THREAD_TITLE_MAX_LENGTH - len(suffix)].rstrip()}{suffix}"
        ensured = await ensure_thread(
            channels,
            request.parent_jid,
            title,
            kind=(
                "issue" if str(conversation.subject.namespace).endswith(":issue") else request.kind
            ),
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
            closed=False,
        )
        try:
            binding = await set_conversation_control_binding(
                binding,
                owner_workspace=request.owner_workspace or request.parent_workspace,
                expected_workspace=conversation.workspace,
            )
        except sqlite3.IntegrityError:
            # Another connection runtime may have claimed this readable name
            # between lookup and persistence. Retry with the next readable
            # suffix instead of exposing an internal conversation ID.
            owner = await get_conversation_control_by_thread(thread_jid)
            if owner is None or owner.conversation_id == request.conversation_id:
                raise
            index += 1
            continue
        if binding.closed:
            await set_thread_closed(channels, binding.thread_jid, closed=True)
        return EnsuredConversationControl(binding=binding, created=ensured.created)


async def sync_conversation_control_state(
    channels: list[Channel],
    conversation_id: ConversationId,
) -> None:
    """Apply a conversation's durable lifecycle intent to its current thread."""
    conversation = await get_conversation(conversation_id)
    if conversation is None:
        raise ValueError(f"Unknown conversation: {conversation_id}")
    binding = await get_conversation_control_binding(conversation_id)
    if binding is not None:
        await set_thread_closed(channels, binding.thread_jid, closed=conversation.control_closed)


async def sync_existing_open_conversation_control(
    channels: list[Channel],
    subject: ConversationSubject,
) -> None:
    """Apply provider-confirmed open intent without creating an absent control."""
    conversation = await get_conversation_for_subject(subject)
    if conversation is None:
        return
    async with conversation_runtime_lock(conversation.id):
        # Terminal retirement uses this fence too. Re-read after acquiring it
        # so a stale ignored callback cannot reopen an archived Discord thread.
        conversation = await get_conversation(conversation.id)
        if conversation is not None and not conversation.control_closed:
            await sync_conversation_control_state(channels, conversation.id)


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

    owner_folder = request.owner_workspace or request.parent_workspace
    placement = resolve_workspace_placement(context.workspaces().values(), owner_folder)
    if placement is None:
        raise ValueError("Conversation policy owner is not registered")
    owner = placement.owner

    control = await ensure_conversation_control(context.channels(), request)
    if control.binding.closed:
        raise ConversationControlClosedError(
            f"Conversation control is closed: {request.conversation_id}"
        )
    conversation = await get_conversation(request.conversation_id)
    if conversation is None:
        raise RuntimeError("Conversation disappeared while placing its workspace")
    if conversation.control_closed:
        raise ConversationControlClosedError(
            f"Conversation control is closed: {request.conversation_id}"
        )
    if conversation.workspace != owner_folder:
        raise ConversationControlWorkspaceChangedError(
            f"Conversation workspace changed from {owner_folder} to {conversation.workspace}"
        )
    folder = routed_conversation_folder(owner_folder, conversation.id)

    profile = WorkspaceProfile(
        jid=control.binding.thread_jid,
        name=f"{owner.name}/{control.binding.title}",
        folder=folder,
        trigger=owner.trigger,
        container_config=owner.container_config,
        security=owner.security,
        is_admin=owner.is_admin,
        added_at=datetime.now(UTC).isoformat(),
    )
    current = context.workspaces().get(profile.jid)
    prior_jid = next(
        (
            jid
            for jid, existing in context.workspaces().items()
            if existing.folder == folder and jid != profile.jid
        ),
        None,
    )
    if prior_jid is not None and context.rebind_workspace is not None:
        await context.rebind_workspace(profile)
    elif prior_jid is not None:
        await context.unregister_workspace(prior_jid)
        await context.register_workspace(profile)
    elif current is None or _workspace_shape(current) != _workspace_shape(profile):
        await context.register_workspace(profile)
    if conversation.session_id is not None:
        await context.bind_session(folder, conversation.session_id)
    return EnsuredConversationWorkspace(profile=profile, control=control)
