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
from pynchy.host.orchestrator.app import PynchyApp
from pynchy.host.orchestrator.dep_factory import make_http_deps
from pynchy.host.orchestrator.webhook_conversations import ConversationWebhookDeps
from pynchy.identifiers import GroupFolder
from pynchy.state import create_task, get_task_by_id, init_test_database, resolve_conversation
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
