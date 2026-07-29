"""Edge contracts for durable terminal task retirement."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.conversation.models import (
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_http_deps
from pynchy.host.orchestrator.webhook_conversations import ConversationWebhookDeps
from pynchy.identifiers import GroupFolder
from pynchy.state import (
    apply_conversation_control_state,
    create_task,
    get_task_by_id,
    get_terminal_conversation_retirement,
    init_test_database,
    resolve_conversation,
)
from tests.test_linear_terminal_cleanup import _active_execution, _conversation_id, _task


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


async def test_host_task_retirement_rejects_a_missing_conversation() -> None:
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    with pytest.raises(RuntimeError, match="lost conversation"):
        await deps.retire_conversation_tasks(ConversationId("missing"))


async def test_host_task_retirement_ignores_non_linear_conversations() -> None:
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("chat"),
            key=ConversationSubjectKey("chat-1"),
        ),
        GroupFolder("project"),
    )
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    await deps.retire_conversation_tasks(conversation.id)


async def test_host_task_retirement_does_not_recover_a_terminal_detached_task() -> None:
    conversation_id = await _conversation_id()
    task = replace(_task("detached", conversation_id), conversation_id=None, status="cancelled")
    await create_task(task)
    await _active_execution(task)
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(),
    ):
        await deps.retire_conversation_tasks(conversation_id)

    persisted_task = await get_task_by_id(task.id)
    assert persisted_task is not None
    assert persisted_task.status == "cancelled"


async def test_host_task_retirement_ignores_execution_from_a_prior_workspace() -> None:
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:project:issue"),
            key=ConversationSubjectKey("moved-issue"),
        ),
        GroupFolder("other"),
    )
    task = replace(_task("moved", conversation.id), conversation_id=str(conversation.id))
    await create_task(task)
    await _active_execution(task)
    deps = make_http_deps(PynchyApp())
    assert isinstance(deps, ConversationWebhookDeps)

    with patch(
        "pynchy.host.orchestrator.terminal_task_retirement.cancel_scheduled_agent_workflow",
        AsyncMock(),
    ):
        await deps.retire_conversation_tasks(conversation.id)


async def test_terminal_retirement_includes_a_bound_task_folder() -> None:
    conversation_id = await _conversation_id()
    bound_folder = routed_conversation_folder("project", conversation_id)
    await create_task(replace(_task("bound", conversation_id), bound_group_folder=bound_folder))
    await apply_conversation_control_state(
        conversation_id,
        closed=True,
        control_state_revision="2026-07-29T20:00:00+00:00",
    )

    retirement = await get_terminal_conversation_retirement(conversation_id)

    assert retirement is not None
    assert GroupFolder(bound_folder) in retirement.runtime_folders
