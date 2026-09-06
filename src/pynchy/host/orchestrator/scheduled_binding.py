"""Durable conversation ownership for scheduled agent tasks."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from pynchy.conversation.api import (  # beartype resolves binding port annotations.
    Conversation,
    ConversationId,
    conversation_runtime_lock,
    dynamic_thread_folder,
    parent_workspace_name,
    routed_conversation_folder,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlClosedError,
    ConversationControlRequest,
    ConversationControlWorkspaceChangedError,
    ConversationWorkspaceContext,
    ensure_conversation_workspace,
)
from pynchy.host.orchestrator.threads import EnsuredThread, set_thread_kind, set_thread_title
from pynchy.host.orchestrator.workspace_config import ensure_runtime_workspace_policy_owner
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)


class ScheduledTaskOwnershipError(RuntimeError):
    """A scheduled task has no durable runtime destination."""


class ScheduledTaskTerminalError(ScheduledTaskOwnershipError):
    """A terminal routed conversation cannot start scheduled work."""


_LINEAR_INPUT_SOURCE_PREFIXES = ("external:linear:", "trusted:linear:")


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

    async def get_scheduled_conversation(
        self, conversation_id: ConversationId
    ) -> Conversation | None: ...

    async def persist_scheduled_task_updates(
        self, task_id: str, updates: dict[str, object]
    ) -> None: ...

    async def cancel_scheduled_task(self, task_id: str) -> None: ...


def _task_thread_name(task: ScheduledTask) -> str:
    if task.derived_thread_name is not None and task.derived_thread_name.strip():
        title = task.derived_thread_name.strip()
        if task.config_job_name is not None:
            _, separator, title_without_parent = title.partition(" | ")
            title = title_without_parent if separator else title
            return title if title.startswith("⚙️ ") else f"⚙️ {title}"
        return title
    raise ScheduledTaskOwnershipError("Scheduled task lacks a managed thread name")


def _is_linear_task(task: ScheduledTask) -> bool:
    return task.input_source.startswith(_LINEAR_INPUT_SOURCE_PREFIXES)


def _existing_named_task_binding(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> tuple[WorkspaceProfile, WorkspaceProfile] | None:
    """Return a task's already-registered child runtime for its current owner."""
    if task.bound_chat_jid is None or task.bound_group_folder is None:
        return None
    profile = deps.workspaces.get(task.bound_chat_jid)
    if profile is None or profile.folder != task.bound_group_folder:
        return None
    placement = resolve_workspace_placement(deps.workspaces.values(), task.group_folder)
    if placement is None:
        raise ScheduledTaskOwnershipError(
            f"Scheduled task owner workspace is unavailable: {task.group_folder}"
        )
    if parent_workspace_name(profile.folder) != placement.owner.folder:
        return None
    return profile, placement.owner


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
    current = deps.workspaces.get(profile.jid)
    if current is not None and current.folder != profile.folder:
        await deps.rebind_workspace(profile)
        return
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
    await set_thread_kind(deps.channels, ensured.jid, "automation")
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
    conversation_id: ConversationId,
) -> tuple[WorkspaceProfile, str]:
    attempt = 0
    while True:
        conversation = await _open_routed_conversation(task, deps, conversation_id)
        placement = resolve_workspace_placement(deps.workspaces.values(), conversation.workspace)
        if placement is None:
            raise ScheduledTaskOwnershipError(
                f"Conversation owner workspace is unavailable: {conversation.workspace}"
            )
        if _is_linear_task(task):
            namespace = str(conversation.subject.namespace)
            if not namespace.startswith("linear:") or not namespace.endswith(":issue"):
                raise ScheduledTaskOwnershipError(
                    "Linear task references a non-Linear issue conversation"
                )
        title = _task_thread_name(task)
        try:
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
                    kind="issue" if _is_linear_task(task) else "automation",
                ),
            )
        except ConversationControlClosedError as exc:
            await deps.cancel_scheduled_task(task.id)
            raise ScheduledTaskTerminalError(
                f"Scheduled task belongs to a terminal conversation: {conversation_id}"
            ) from exc
        except ConversationControlWorkspaceChangedError as exc:
            if attempt == 1:
                raise ScheduledTaskOwnershipError(
                    "Conversation workspace changed while binding scheduled work"
                ) from exc
            attempt = 1
            continue
        if ensured.control.binding.closed:
            await deps.cancel_scheduled_task(task.id)
            raise ScheduledTaskTerminalError(
                f"Scheduled task belongs to a terminal conversation: {conversation_id}"
            )
        if _is_linear_task(task):
            # Linear controller-created conversations bypass webhook dispatcher registration.
            # Agent preflight needs this exact owner mapping to resolve inherited repo mounts.
            ensure_runtime_workspace_policy_owner(
                routed_conversation_folder(conversation.workspace, conversation.id),
                placement.owner.folder,
            )
        return ensured.profile, ensured.control.binding.title


async def _open_routed_conversation(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
    conversation_id: ConversationId,
) -> Conversation:
    """Return a routed task's open durable conversation or cancel its task."""
    conversation = await deps.get_scheduled_conversation(conversation_id)
    if conversation is None:
        raise ScheduledTaskOwnershipError(
            f"Scheduled task references a missing conversation: {conversation_id}"
        )
    if conversation.control_closed:
        await deps.cancel_scheduled_task(task.id)
        raise ScheduledTaskTerminalError(
            f"Scheduled task belongs to a terminal conversation: {conversation_id}"
        )
    return conversation


async def ensure_scheduled_task_conversation_open(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> None:
    """Reject queued scheduled work after its routed conversation became terminal."""
    if task.conversation_id is None:
        return
    conversation_id = ConversationId(task.conversation_id)
    async with conversation_runtime_lock(conversation_id):
        await _open_routed_conversation(task, deps, conversation_id)


async def _bind_task_runtime(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> tuple[WorkspaceProfile, str]:
    if task.conversation_id is not None:
        return await _bind_routed_conversation(
            task,
            deps,
            ConversationId(task.conversation_id),
        )
    existing = _existing_named_task_binding(task, deps)
    if existing is not None:
        profile, owner = existing
        title = _task_thread_name(task)
        await set_thread_title(deps.channels, profile.jid, title)
        desired_name = f"{owner.name}/{title}"
        if profile.name != desired_name:
            profile = replace(profile, name=desired_name)
            await deps.register_workspace(profile)
        return profile, title
    return await _bind_named_thread(task, deps)


async def ensure_scheduled_task_binding(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> ScheduledTask:
    """Persist and return the one thread runtime owned by a scheduled task."""
    if task.conversation_id is not None:
        conversation_id = ConversationId(task.conversation_id)
        async with conversation_runtime_lock(conversation_id):
            bound = await _ensure_scheduled_task_binding(task, deps)
            await _open_routed_conversation(bound, deps, conversation_id)
            return bound
    return await _ensure_scheduled_task_binding(task, deps)


async def _ensure_scheduled_task_binding(
    task: ScheduledTask,
    deps: ScheduledBindingDeps,
) -> ScheduledTask:
    """Bind one task while its routed conversation runtime fence is held."""
    ownership_updates: dict[str, object] = {}
    if _is_linear_task(task):
        if task.session_policy is not SessionPolicy.CONTINUE:
            task = replace(task, session_policy=SessionPolicy.CONTINUE)
            ownership_updates["session_policy"] = SessionPolicy.CONTINUE
        if task.conversation_id is None:
            raise ScheduledTaskOwnershipError("Linear task has no durable issue conversation")
    if ownership_updates:
        await deps.persist_scheduled_task_updates(task.id, ownership_updates)

    profile, title = await _bind_task_runtime(task, deps)
    updates: dict[str, object] = {}
    if task.bound_chat_jid != profile.jid:
        updates["bound_chat_jid"] = profile.jid
    if task.bound_group_folder != profile.folder:
        updates["bound_group_folder"] = profile.folder
    if task.derived_thread_name != title:
        updates["derived_thread_name"] = title
    if updates:
        await deps.persist_scheduled_task_updates(task.id, updates)
        task = replace(
            task,
            bound_chat_jid=profile.jid,
            bound_group_folder=profile.folder,
            derived_thread_name=title,
        )
    return task


async def reconcile_scheduled_task_bindings(
    tasks: list[ScheduledTask],
    deps: ScheduledBindingDeps,
) -> int:
    """Eagerly create or repair automation posts for active and paused tasks."""
    reconciled = 0
    for task in tasks:
        if task.status not in {"active", "paused"} or task.conversation_id is not None:
            continue
        try:
            await ensure_scheduled_task_binding(task, deps)
        except Exception as exc:  # noqa: BLE001 - one invalid task must not block startup.
            logger.warning(
                "Scheduled task binding reconciliation failed",
                task_id=task.id,
                group_folder=task.group_folder,
                exc_type=type(exc).__name__,
                err=str(exc),
            )
            continue
        reconciled += 1
    return reconciled
