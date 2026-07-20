"""Reconcile replaceable Discord controls for durable routed conversations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pynchy.config.workspace_names import parent_workspace_name
from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
)
from pynchy.host.orchestrator.threads import ensure_thread
from pynchy.state import (
    get_conversation,
    get_workspace_profile,
    set_conversation_control_binding,
)
from pynchy.types import Channel, ChatJid, GroupFolder


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

    ensured = await ensure_thread(
        channels,
        request.parent_jid,
        request.title,
    )
    if ensured.jid is None:
        raise RuntimeError("Ensured conversation control returned no chat JID")

    binding = ConversationControlBinding(
        conversation_id=request.conversation_id,
        surface=ControlSurface.DISCORD,
        parent_workspace=request.parent_workspace,
        parent_jid=request.parent_jid,
        thread_jid=ChatJid(ensured.jid),
        title=request.title,
        updated_at=datetime.now(UTC).isoformat(),
    )
    await set_conversation_control_binding(binding)
    return EnsuredConversationControl(binding=binding, created=ensured.created)
