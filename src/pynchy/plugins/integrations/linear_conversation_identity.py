"""Canonical routed identity for Linear issue conversations."""

from __future__ import annotations

from collections.abc import (  # noqa: TC003 - beartype resolves configured state callbacks at runtime.
    Awaitable,
    Callable,
)
from dataclasses import dataclass

from pynchy.conversation.api import (
    Conversation,
    ConversationControlBinding,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.identifiers import GroupFolder
from pynchy.work_items.api import (
    WorkItemExecution,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)


@dataclass(frozen=True)
class LinearConversationRuntime:
    """Durable conversation operations selected during plugin composition."""

    get_unfinished_execution: Callable[[str], Awaitable[WorkItemExecution | None]]
    get_for_subject_key: Callable[
        [ConversationSubjectKey, GroupFolder, str], Awaitable[Conversation | None]
    ]
    get_control_binding: Callable[[ConversationId], Awaitable[ConversationControlBinding | None]]
    resolve: Callable[[ConversationSubject, GroupFolder], Awaitable[Conversation]]


@dataclass
class _RuntimeState:
    runtime: LinearConversationRuntime | None = None


_runtime = _RuntimeState()


def configure_linear_conversation_runtime(runtime: LinearConversationRuntime) -> None:
    """Set the durable conversation operations used by Linear routes."""
    _runtime.runtime = runtime


def _configured_runtime() -> LinearConversationRuntime:
    if _runtime.runtime is None:
        raise RuntimeError("Linear conversation runtime has not been configured")
    return _runtime.runtime


async def resolve_linear_issue_conversation(
    issue_id: str,
    workspace: str,
    account_name: str,
) -> Conversation:
    """Return one issue runtime, pinned to any unfinished execution's workspace."""
    runtime = _configured_runtime()
    execution = await runtime.get_unfinished_execution(issue_id)
    owner_workspace = execution.workspace if execution is not None else workspace
    group_folder = GroupFolder(owner_workspace)
    existing = await runtime.get_for_subject_key(
        ConversationSubjectKey(issue_id),
        group_folder,
        ":issue",
    )
    if existing is not None:
        return existing
    return await runtime.resolve(
        ConversationSubject(
            namespace=ConversationSubjectNamespace(f"linear:{account_name}:issue"),
            key=ConversationSubjectKey(issue_id),
        ),
        group_folder,
    )


async def find_linear_issue_control_conversation(
    issue_id: str,
    workspace: str,
) -> tuple[Conversation, ConversationControlBinding] | None:
    """Return an existing issue conversation only when it already has a control."""
    runtime = _configured_runtime()
    execution = await runtime.get_unfinished_execution(issue_id)
    owner_workspace = execution.workspace if execution is not None else workspace
    existing = await runtime.get_for_subject_key(
        ConversationSubjectKey(issue_id),
        GroupFolder(owner_workspace),
        ":issue",
    )
    if existing is None:
        return None
    binding = await runtime.get_control_binding(existing.id)
    return (existing, binding) if binding is not None and not binding.closed else None
