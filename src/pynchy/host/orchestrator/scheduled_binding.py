"""Durable conversation ownership for scheduled agent tasks."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pynchy.config.workspace_names import dynamic_thread_folder
from pynchy.conversation.models import ConversationId
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    ensure_conversation_workspace,
)
from pynchy.host.orchestrator.threads import EnsuredThread  # noqa: TC001, RUF100
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.state import get_conversation, update_task
from pynchy.types import (
    Channel,
    ChatJid,
    GroupFolder,
    ScheduledTask,
    SessionId,
    SessionPolicy,
    WorkspaceProfile,
)


class ScheduledTaskOwnershipError(RuntimeError):
    """A scheduled task has no durable runtime destination."""


@runtime_checkable
class ScheduledBindingDeps(Protocol):
    @property
    def workspaces(self) -> dict[str, WorkspaceProfile]: ...

    @property
    def channels(self) -> list[Channel]: ...

    async def ensure_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> EnsuredThread: ...

    async def register_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def unregister_workspace(self, jid: str) -> None: ...

    async def rebind_workspace(self, profile: WorkspaceProfile) -> None: ...

    async def bind_routed_session(self, group_folder: str, session_id: SessionId) -> None: ...


def _task_thread_name(task: ScheduledTask) -> str:
    if task.derived_thread_name is not None and task.derived_thread_name.strip():
        return task.derived_thread_name
    return f"Scheduled | {task.id}"[:100]


def resolve_scheduled_group(
    workspaces: dict[str, WorkspaceProfile],
    group_folder: str,
) -> WorkspaceProfile | None:
    """Resolve a bound runtime folder to its registered workspace owner."""
    exact = next(
        (group for group in workspaces.values() if group.folder == group_folder),
        None,
    )
    if exact is not None:
        return exact
    placement = resolve_workspace_placement(workspaces.values(), group_folder)
    return placement.owner if placement is not None else None


async def _register_bound_profile(
    deps: ScheduledBindingDeps,
    profile: WorkspaceProfile,
) -> None:
    prior_jid = next(
        (
            jid
            for jid, existing in deps.workspaces.items()
            if existing.folder == profile.folder and jid != profile.jid
        ),
        None,
    )
    if prior_jid is not None:
        await deps.rebind_workspace(profile)
        return
    if deps.workspaces.get(profile.jid) != profile:
        await deps.register_workspace(profile)


async def _bind_named_thread(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> tuple[WorkspaceProfile, str]:
    placement = resolve_workspace_placement(deps.workspaces.values(), task.group_folder)
    if placement is None:
        raise ScheduledTaskOwnershipError(
            f"Scheduled task owner workspace is unavailable: {task.group_folder}"
        )
    title = _task_thread_name(task)
    ensured = await deps.ensure_thread(placement.control_parent.jid, title)
    if ensured.jid is None:
        raise ScheduledTaskOwnershipError("Scheduled task thread creation returned no chat JID")
    profile = replace(
        placement.owner,
        jid=ensured.jid,
        name=f"{placement.owner.name}/{title}",
        folder=dynamic_thread_folder(placement.owner.folder, ensured.jid),
        added_at=datetime.now(UTC).isoformat(),
    )
    await _register_bound_profile(deps, profile)
    return profile, title


async def _bind_routed_conversation(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> tuple[WorkspaceProfile, str]:
    if task.conversation_id is None:
        raise ScheduledTaskOwnershipError("Routed task lost its conversation identity")
    conversation = await get_conversation(ConversationId(task.conversation_id))
    if conversation is None:
        raise ScheduledTaskOwnershipError(
            f"Scheduled task references a missing conversation: {task.conversation_id}"
        )
    placement = resolve_workspace_placement(deps.workspaces.values(), conversation.workspace)
    if placement is None:
        raise ScheduledTaskOwnershipError(
            f"Conversation owner workspace is unavailable: {conversation.workspace}"
        )
    title = _task_thread_name(task)
    ensured = await ensure_conversation_workspace(
        ConversationWorkspaceContext(
            channels=lambda: deps.channels,
            workspaces=lambda: deps.workspaces,
            register_workspace=deps.register_workspace,
            unregister_workspace=deps.unregister_workspace,
            bind_session=deps.bind_routed_session,
            rebind_workspace=deps.rebind_workspace,
        ),
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder(placement.control_parent.folder),
            parent_jid=ChatJid(placement.control_parent.jid),
            title=title,
            owner_workspace=conversation.workspace,
            closed=False,
        ),
    )
    return ensured.profile, ensured.control.binding.title


async def ensure_scheduled_task_binding(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> ScheduledTask:
    """Persist and return the one thread runtime owned by a scheduled task."""
    ownership_updates: dict[str, object] = {}
    if ":linear:" in task.input_source:
        if task.session_policy is not SessionPolicy.CONTINUE:
            task = replace(task, session_policy=SessionPolicy.CONTINUE)
            ownership_updates["session_policy"] = SessionPolicy.CONTINUE
        if task.conversation_id is None:
            raise ScheduledTaskOwnershipError("Linear task has no durable issue conversation")
    if ownership_updates:
        await update_task(task.id, ownership_updates)

    profile, title = (
        await _bind_routed_conversation(task, deps)
        if task.conversation_id is not None
        else await _bind_named_thread(task, deps)
    )
    updates: dict[str, object] = {}
    if task.bound_chat_jid != profile.jid:
        updates["bound_chat_jid"] = profile.jid
    if task.bound_group_folder != profile.folder:
        updates["bound_group_folder"] = profile.folder
    if task.derived_thread_name != title:
        updates["derived_thread_name"] = title
    if updates:
        await update_task(task.id, updates)
        task = replace(
            task,
            bound_chat_jid=profile.jid,
            bound_group_folder=profile.folder,
            derived_thread_name=title,
        )
    if task.bound_chat_jid is None or task.bound_group_folder is None:
        raise ScheduledTaskOwnershipError("Scheduled task destination binding was not persisted")
    return task
