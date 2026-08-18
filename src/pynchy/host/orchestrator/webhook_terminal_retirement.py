"""Retire local webhook runtime after a provider terminal lifecycle event."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pynchy.conversation.api import (  # beartype resolves runtime annotations.
    ConversationClaimId,
    ConversationId,
    ExternalDeliveryIdentity,
    TerminalConversationRetirement,
    conversation_id_from_folder,
    conversation_runtime_lock,
)
from pynchy.host.orchestrator.workspace_config import unregister_runtime_workspace_policy
from pynchy.identifiers import (
    GroupFolder,  # beartype resolves runtime annotations.
)


@runtime_checkable
class TerminalConversationRetirementDeps(Protocol):
    """Host operations required to stop a terminal routed conversation."""

    async def unregister_workspace(self, jid: str) -> None: ...

    async def retire_conversation_runtime(self, folder: GroupFolder) -> None: ...

    async def retire_conversation_tasks(self, conversation_id: ConversationId) -> None: ...

    async def conversation_control_state_matches(
        self,
        conversation_id: ConversationId,
        *,
        closed: bool,
        control_state_revision: str | None,
        delivery_identity: ExternalDeliveryIdentity | None = None,
        claim_id: ConversationClaimId | None = None,
    ) -> bool: ...


@runtime_checkable
class TerminalConversationRecoveryDeps(TerminalConversationRetirementDeps, Protocol):
    """Host capabilities that reconcile terminal cleanup after a restart."""

    async def get_terminal_conversation_retirement(
        self,
        conversation_id: ConversationId,
    ) -> TerminalConversationRetirement | None: ...


async def recover_terminal_conversation(
    deps: TerminalConversationRecoveryDeps,
    conversation_id: ConversationId,
    runtime_workspace_folders: set[str],
) -> bool:
    """Apply durable terminal state to local runtime cleanup."""
    retirement = await deps.get_terminal_conversation_retirement(conversation_id)
    if retirement is None:
        return False
    return await retire_terminal_runtime(
        deps,
        conversation_id,
        retirement,
        runtime_workspace_folders,
    )


async def retire_terminal_runtime(
    deps: TerminalConversationRetirementDeps,
    conversation_id: ConversationId,
    retirement: TerminalConversationRetirement,
    runtime_workspace_folders: set[str],
) -> bool:
    """Release local runtime only while durable terminal intent still wins."""
    if not retirement.is_current:
        return False
    async with conversation_runtime_lock(conversation_id):
        if not await deps.conversation_control_state_matches(
            conversation_id,
            closed=True,
            control_state_revision=retirement.control_state_revision,
        ):
            return False
        runtime_folders = set(retirement.runtime_folders)
        runtime_folders.update(
            GroupFolder(folder)
            for folder in runtime_workspace_folders
            if conversation_id_from_folder(folder) == conversation_id
        )
        for runtime_folder in sorted(runtime_folders):
            await deps.retire_conversation_runtime(runtime_folder)
        await deps.retire_conversation_tasks(conversation_id)
        for workspace_jid in retirement.runtime_workspace_jids:
            await deps.unregister_workspace(workspace_jid)
        for runtime_folder in runtime_folders:
            unregister_runtime_workspace_policy(runtime_folder)
            runtime_workspace_folders.discard(runtime_folder)
    return True
