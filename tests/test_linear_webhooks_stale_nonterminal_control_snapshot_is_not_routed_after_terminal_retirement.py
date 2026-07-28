"""Behavioral coverage for authenticated Linear issue-conversation admission."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from unittest.mock import ANY, AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import (
    SIGNING_KEY as _SIGNING_KEY,
)
from linear_webhook_test_support import (
    LinearWebhookHarness as _WebhookDeps,
)
from linear_webhook_test_support import (
    payload as _payload,
)
from linear_webhook_test_support import (
    post_linear_event as _post_linear_event,
)
from linear_webhook_test_support import (
    public_runtime as _public_runtime,
)
from linear_webhook_test_support import (
    route_config as _config,
)
from linear_webhook_test_support import (
    signed_request as _signed_request,
)
from linear_webhook_test_support import (
    webhook_route as _route,
)

from pynchy.conversation.dispatch import conversation_runtime_lock
from pynchy.conversation.models import (
    ConversationSubjectKey,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.identifiers import (
    GroupFolder,
    SessionId,
)
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_webhook_effects import (
    process_linear_webhook_event,
    process_linear_webhook_lifecycle,
)
from pynchy.plugins.integrations.linear_webhooks import (
    parse_linear_webhook,
    prepare_linear_webhook_event,
)
from pynchy.state import (
    apply_conversation_control_state,
    get_active_work_item_execution,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_for_subject_key,
    get_work_item_execution_for_issue,
    resolve_conversation,
)
from pynchy.work_items.api import (
    WorkItemExecutionStatus,
)
from tests.linear_webhooks_support import (
    _linear_client_context,
    _reconcile_client_context,
    _seed_moved_active_issue,
    _workspace_board,
)

pytest_plugins = ("tests.linear_webhooks_support",)


async def test_stale_nonterminal_control_snapshot_is_not_routed_after_terminal_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Stale nonterminal callback",
                "state": {"id": "state-started", "name": "In Progress", "type": "started"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(
        event,
        conversation=replace(
            event.conversation,
            workspace="project",
            control_state_revision="2026-07-27T00:00:00+00:00",
        ),
    )
    conversation = await resolve_conversation(event.conversation.subject, GroupFolder("project"))
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision="2026-07-27T00:00:01+00:00",
    )
    controller = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects._controller_owns_event",
        controller,
    )

    processed = await process_linear_webhook_event(event)

    retired = await get_conversation(conversation.id)
    assert retired is not None
    assert retired.control_closed is True
    assert retired.control_state_revision == "2026-07-27T00:00:01+00:00"
    assert processed.conversation is None
    assert processed.ignored_reason == "stale_linear_control_state"
    controller.assert_not_awaited()


async def test_current_nonterminal_comment_reopens_a_terminal_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now))
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    conversation = await resolve_conversation(event.conversation.subject, GroupFolder("project"))
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision="2026-07-27T00:00:01+00:00",
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        AsyncMock(
            return_value=(
                {
                    "id": "issue-1",
                    "updatedAt": "2026-07-27T00:00:02+00:00",
                    "state": {
                        "id": "state-started",
                        "name": "In Progress",
                        "type": "started",
                    },
                },
                _workspace_board(),
            )
        ),
    )

    prepared = await prepare_linear_webhook_event(event, config=_config())
    processed = await process_linear_webhook_event(prepared)
    reopened = await get_conversation(conversation.id)

    assert processed.conversation is not None
    assert reopened is not None
    assert reopened.control_closed is False
    assert reopened.control_state_revision == "2026-07-27T00:00:02+00:00"


async def test_controller_work_waits_for_terminal_fence_after_reopen_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Resume controller work",
                "state": {"id": "state-started", "name": "In Progress", "type": "started"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(
        event,
        conversation=replace(
            event.conversation,
            workspace="project",
            control_closed=False,
            control_state_revision="2026-07-27T00:00:02+00:00",
        ),
    )
    conversation = await resolve_conversation(event.conversation.subject, GroupFolder("project"))
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision="2026-07-27T00:00:01+00:00",
    )
    controller = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects._controller_owns_event",
        controller,
    )

    async with conversation_runtime_lock(conversation.id):
        task = asyncio.create_task(process_linear_webhook_event(event))
        for _ in range(50):
            reopened = await get_conversation(conversation.id)
            if (
                reopened is not None
                and not reopened.control_closed
                and reopened.control_state_revision == "2026-07-27T00:00:02+00:00"
            ):
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("nonterminal control state was not committed before controller work")
        controller.assert_not_awaited()
        assert not task.done()

    processed = await task
    reopened = await get_conversation(conversation.id)

    assert processed.conversation is not None
    assert reopened is not None
    assert reopened.control_closed is False
    controller.assert_awaited_once_with(event, "project")


async def test_moved_active_issue_comment_reuses_original_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    original, destination = await _seed_moved_active_issue(deps)

    config = _config().model_copy(update={"workspace": destination.folder})
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        AsyncMock(return_value=({"id": "issue-1"}, _workspace_board())),
    )
    route = replace(
        _route(),
        workspace=None,
        candidate_workspaces=(deps.workspace.folder, destination.folder),
        prepare_event=partial(
            prepare_linear_webhook_event,
            config=config,
            public_source=False,
        ),
    )
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, body = await _post_linear_event(client, _payload(now=datetime.now(UTC)))
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "accepted", "duplicate": False}
    assert len(deps.ingested) == 1
    assert deps.ingested[0].chat_jid == "discord:channel:original-thread"
    assert deps.ingested[0].metadata["conversation_id"] == original.id
    assert not deps.channel.created
    assert (
        await get_conversation_for_subject_key(
            ConversationSubjectKey("issue-1"),
            workspace=GroupFolder(destination.folder),
            namespace_suffix=":issue",
        )
        is None
    )
    preserved = await get_conversation(original.id)
    assert preserved is not None
    assert preserved.workspace == deps.workspace.folder
    assert preserved.session_id == SessionId("original-session")


async def test_moved_active_issue_update_uses_destination_board_and_original_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    original, destination = await _seed_moved_active_issue(deps)
    revision = datetime.now(UTC).isoformat()
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "destination-project"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    prepare_workspace_issue = AsyncMock(
        return_value=(
            {
                "id": "issue-1",
                "updatedAt": revision,
                "state": {
                    "id": "state-progress",
                    "name": "In Progress",
                    "type": "started",
                },
            },
            board,
        )
    )
    process_workspace_issue = AsyncMock(return_value=({"state": {"id": "state-progress"}}, board))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        prepare_workspace_issue,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        process_workspace_issue,
    )
    config = _config().model_copy(update={"workspace": destination.folder})
    route = replace(
        _route(),
        workspace=None,
        candidate_workspaces=(deps.workspace.folder, destination.folder),
        prepare_event=partial(
            prepare_linear_webhook_event,
            config=config,
            public_source=False,
        ),
        process_event=process_linear_webhook_event,
    )
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, body = await _post_linear_event(
            client,
            _payload(
                now=datetime.now(UTC),
                event_type="Issue",
                action="update",
                data={
                    "id": "issue-1",
                    "identifier": "PYN-1",
                    "title": "Moved active work",
                    "updatedAt": revision,
                    "state": {
                        "id": "state-progress",
                        "name": "In Progress",
                        "type": "started",
                    },
                },
            ),
        )
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "ignored", "duplicate": False}
    process_workspace_issue.assert_awaited_once_with(ANY, destination.folder, "issue-1")
    assert not deps.ingested
    assert not deps.channel.created
    binding = await get_conversation_control_binding(original.id)
    assert binding is not None
    assert binding.thread_jid == "discord:channel:original-thread"
    assert (
        await get_conversation_for_subject_key(
            ConversationSubjectKey("issue-1"),
            workspace=GroupFolder(destination.folder),
            namespace_suffix=":issue",
        )
        is None
    )
    preserved = await get_conversation(original.id)
    assert preserved is not None
    assert preserved.workspace == deps.workspace.folder
    assert preserved.session_id == SessionId("original-session")


@pytest.mark.parametrize(
    "execution_status",
    [
        WorkItemExecutionStatus.IN_PROGRESS,
        WorkItemExecutionStatus.AWAITING_REVIEW,
        WorkItemExecutionStatus.FOLLOW_UPS,
        WorkItemExecutionStatus.BLOCKED,
    ],
)
async def test_moved_unfinished_issue_done_reconciles_against_destination_board(
    monkeypatch: pytest.MonkeyPatch,
    execution_status: WorkItemExecutionStatus,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    original, destination = await _seed_moved_active_issue(
        deps,
        execution_status=execution_status,
    )
    destination_board = _workspace_board()
    prepare_workspace_issue = AsyncMock(return_value=({"id": "issue-1"}, destination_board))
    complete_workspace_issue = AsyncMock(
        return_value=(
            {
                "id": "issue-1",
                "identifier": "PYN-1",
                "url": "https://linear.app/acme/issue/PYN-1",
                "updatedAt": datetime.now(UTC).isoformat(),
                "state": {"id": "state-done", "name": "Done"},
            },
            destination_board,
        )
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        prepare_workspace_issue,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_completion.linear_client",
        lambda **_kwargs: _reconcile_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_work_item_provider.workspace_issue",
        complete_workspace_issue,
    )
    config = _config().model_copy(update={"workspace": destination.folder})
    route = replace(
        _route(),
        workspace=None,
        candidate_workspaces=(deps.workspace.folder, destination.folder),
        prepare_event=partial(
            prepare_linear_webhook_event,
            config=config,
            public_source=False,
        ),
        process_lifecycle=process_linear_webhook_lifecycle,
    )
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, body = await _post_linear_event(
            client,
            _payload(
                now=datetime.now(UTC),
                event_type="Issue",
                action="update",
                data={
                    "id": "issue-1",
                    "identifier": "PYN-1",
                    "title": "Moved active work",
                    "state": {"id": "state-done", "name": "Done", "type": "completed"},
                },
            ),
        )
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "accepted", "duplicate": False}
    complete_workspace_issue.assert_awaited_once_with(ANY, destination.folder, "issue-1")
    assert await get_active_work_item_execution("issue-1") is None
    completed = await get_work_item_execution_for_issue(
        "issue-1",
        workspace=deps.workspace.folder,
    )
    assert completed is not None
    assert completed.status is WorkItemExecutionStatus.COMPLETED
    binding = await get_conversation_control_binding(original.id)
    assert binding is not None
    assert binding.thread_jid == "discord:channel:original-thread"
    assert binding.closed is True
    assert deps.channel.closed["discord:channel:original-thread"] is True
    assert not deps.ingested
    assert not deps.channel.created
    assert (
        await get_conversation_for_subject_key(
            ConversationSubjectKey("issue-1"),
            workspace=GroupFolder(destination.folder),
            namespace_suffix=":issue",
        )
        is None
    )
