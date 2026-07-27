"""Behavioral coverage for authenticated Linear issue-conversation admission."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from conftest import make_settings
from linear_webhook_test_support import (
    DELIVERY_ID as _DELIVERY_ID,
)
from linear_webhook_test_support import (
    SECOND_DELIVERY_ID as _SECOND_DELIVERY_ID,
)
from linear_webhook_test_support import (
    SIGNING_KEY as _SIGNING_KEY,
)
from linear_webhook_test_support import (
    THIRD_DELIVERY_ID as _THIRD_DELIVERY_ID,
)
from linear_webhook_test_support import (
    CursorDeps as _CursorDeps,
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

from pynchy.config import PluginConfig
from pynchy.config.models import LinearTool, ProfileConfig, WorkspaceConfig
from pynchy.conversation.models import (
    ControlSurface,
    Conversation,
    ConversationControlBinding,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.messaging.cursor import complete_turn_with_cursor
from pynchy.host.orchestrator.webhook_event_payloads import (
    webhook_event_from_payload,
    webhook_event_payload,
)
from pynchy.plugins.integrations.linear import LinearMcpPlugin
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_client import LinearError
from pynchy.plugins.integrations.linear_webhook_effects import (
    process_linear_webhook_event,
    process_linear_webhook_lifecycle,
)
from pynchy.plugins.integrations.linear_webhooks import (
    LinearWebhookRouteConfig,
    parse_linear_webhook,
    prepare_linear_webhook_event,
)
from pynchy.plugins.integrations.linear_work_item_provider import LinearWorkspaceIssueError
from pynchy.plugins.webhooks import (
    WebhookAuthenticationError,
    WebhookConfigurationError,
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycleDelivery,
    WebhookProcessingError,
)
from pynchy.state import (
    WorkItemClaimRequest,
    create_work_item_claim,
    get_all_tasks,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_for_subject_key,
    get_webhook_receipt,
    get_work_item_transition_by_request,
    init_test_database,
    resolve_conversation,
    resolve_work_item_transition,
    set_conversation_control_binding,
    set_conversation_session,
)
from pynchy.types import (
    GroupFolder,
    SessionId,
    WorkItemExecutionStatus,
    WorkItemTransitionStatus,
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass(frozen=True)
class _LeaseResult:
    status: WorkItemExecutionStatus


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def test_deferred_linear_webhook_preserves_controller_workspace() -> None:
    event = WebhookEvent(
        delivery_id=_DELIVERY_ID,
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at=datetime.now(UTC).isoformat(),
        instructions="Handle the provider update.",
        external_context="Issue update",
        conversation=WebhookConversation(
            subject=ConversationSubject(
                namespace=ConversationSubjectNamespace("linear:org-1:issue"),
                key=ConversationSubjectKey("issue-1"),
            ),
            control_title="[PYN-1] Linear issue",
            workspace="pynchy-work",
            controller_workspace="pynchy-dev",
        ),
    )

    restored = webhook_event_from_payload(webhook_event_payload(event))

    assert restored.conversation is not None
    assert restored.conversation.workspace == "pynchy-work"
    assert restored.conversation.controller_workspace == "pynchy-dev"


def _workspace_board() -> LinearWorkspaceBoard:
    return LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={"done": {"id": "state-done"}},
    )


@pytest.mark.parametrize(
    ("action", "activity"),
    [
        ("create", "new comment was posted"),
        ("update", "comment was edited"),
        ("remove", "comment was removed"),
    ],
)
def test_every_comment_change_maps_to_concise_issue_conversation(
    action: str,
    activity: str,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now, action=action))

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.subject_id == "issue-1"
    assert event.instructions is not None
    assert activity in event.instructions
    assert "current authorization" in event.instructions
    assert event.external_context is not None
    context_activity = {"create": "posted", "update": "edited", "remove": "removed"}[action]
    assert f"Event: comment {context_activity}" in event.external_context
    assert "Issue: PYN-1" in event.external_context
    assert "Author: Example User" in event.external_context
    assert "Comment:\nplease review this" in event.external_context
    assert event.conversation is not None
    assert event.conversation.subject.namespace == "linear:org-1:issue"
    assert event.conversation.subject.key == "issue-1"
    assert event.conversation.control_title == "[PYN-1] Linear issue"
    assert event.conversation.control_closed is None


@pytest.mark.parametrize(
    ("action", "updated_from"),
    [("create", None), ("update", {"title": "Old title"}), ("remove", None)],
)
def test_every_issue_change_targets_the_issue_conversation(
    action: str,
    updated_from: dict[str, object] | None,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action=action,
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Webhook callbacks",
                "state": {"id": "state-1", "name": "In Progress", "type": "started"},
            },
            updated_from=updated_from,
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is not None
    assert event.external_context is not None
    assert "current authorization" in event.instructions
    assert f"Event: issue {action}" in event.external_context
    assert "State: In Progress" in event.external_context
    assert (
        "Changed fields: title" if updated_from is not None else "Changed fields: none reported"
    ) in event.external_context
    assert event.actor is not None
    assert event.actor.id == "user-1"
    assert event.actor.kind == "user"
    assert event.changed_fields == frozenset(updated_from or ())
    assert event.conversation is not None
    assert event.conversation.control_title == "[PYN-1] Webhook callbacks"
    assert event.conversation.control_closed is False


def test_agent_proposed_issue_creation_does_not_authorize_or_wake_work() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="create",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Created issue",
                "state": {
                    "id": "state-agent-proposed",
                    "name": "Agent Proposed",
                    "type": "backlog",
                },
            },
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is None
    assert event.external_context is None
    assert event.conversation is None
    assert event.ignored_reason == "issue_creation_does_not_authorize_work"


def test_nested_agent_proposed_issue_creation_does_not_authorize_or_wake_work() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="create",
            data={
                "id": "issue-1",
                "issue": {
                    "state": {
                        "id": "state-agent-proposed",
                        "name": " agent proposed ",
                        "type": "backlog",
                    },
                },
            },
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is None
    assert event.external_context is None
    assert event.conversation is None
    assert event.ignored_reason == "issue_creation_does_not_authorize_work"


@pytest.mark.parametrize(
    ("state", "expected_context"),
    [
        (
            {"id": "state-done", "name": "Done", "type": "completed"},
            {"linear_state_id": "state-done"},
        ),
        (
            {"id": "state-duplicate", "name": "Duplicate", "type": "canceled"},
            {"linear_state_id": "state-duplicate"},
        ),
        (
            {"id": "state-custom", "name": "Shipped", "type": "completed"},
            {"linear_state_id": "state-custom"},
        ),
    ],
)
def test_typed_terminal_issue_states_become_lifecycle_only(
    state: dict[str, str],
    expected_context: dict[str, str],
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
                "title": "Reviewed outcome",
                "state": state,
            },
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is None
    assert event.external_context is None
    assert event.lifecycle is not None
    assert event.lifecycle.context == expected_context
    assert event.conversation is not None
    assert event.conversation.control_closed is True


def test_nested_typed_terminal_issue_state_becomes_lifecycle_only() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "issue": {
                    "id": "issue-1",
                    "identifier": "PYN-1",
                    "title": "Nested state",
                    "state": {"id": "state-canceled", "name": "Canceled", "type": "canceled"},
                },
            },
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.lifecycle is not None
    assert event.lifecycle.context == {"linear_state_id": "state-canceled"}
    assert event.conversation is not None
    assert event.conversation.control_closed is True


@pytest.mark.parametrize(
    "state",
    [
        {"id": "state-done", "name": "Done"},
        {"id": "state-done", "name": "Done", "type": "started"},
    ],
)
def test_display_name_does_not_make_an_issue_terminal(state: dict[str, str]) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Not terminal",
                "state": state,
            },
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.lifecycle is None
    assert event.instructions is not None
    assert event.conversation is not None
    assert event.conversation.control_closed is (False if "type" in state else None)


async def test_terminal_lifecycle_preparation_resolves_its_workspace(
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
                "title": "Terminal event",
                "state": {"id": "state-done", "name": "Done", "type": "completed"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.lifecycle is not None

    workspace_issue = AsyncMock(return_value=({"id": "issue-1"}, _workspace_board()))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        workspace_issue,
    )

    prepared = await prepare_linear_webhook_event(event, config=_config())

    assert prepared.lifecycle is not None
    assert prepared.lifecycle.context == {
        "linear_state_id": "state-done",
        "linear_managed_done_state_id": "state-done",
    }
    assert prepared.conversation is not None
    assert prepared.conversation.workspace == "project"
    workspace_issue.assert_awaited_once()


async def _seed_moved_active_issue(
    deps: _WebhookDeps,
) -> tuple[Conversation, WorkspaceProfile]:
    destination = WorkspaceProfile(
        jid="discord:channel:destination",
        name="Destination",
        folder="destination",
        trigger="@Pynchy",
    )
    await deps.register_workspace(destination)
    original = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:org-1:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(deps.workspace.folder),
    )
    await set_conversation_session(original.id, SessionId("original-session"))
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=original.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder(deps.workspace.folder),
            parent_jid=deps.workspace.jid,
            thread_jid="discord:channel:original-thread",
            title="[PYN-1] Linear issue",
            updated_at=datetime.now(UTC).isoformat(),
        )
    )
    deps.channel.threads[deps.workspace.jid, "[PYN-1] Linear issue"] = (
        "discord:channel:original-thread"
    )
    issue = {
        "id": "issue-1",
        "identifier": "PYN-1",
        "url": "https://linear.app/acme/issue/PYN-1",
        "updatedAt": datetime.now(UTC).isoformat(),
        "state": {"id": "state-approved", "name": "Human Approved"},
    }
    await create_work_item_claim(
        WorkItemClaimRequest(
            workspace=deps.workspace.folder,
            issue=issue,
            turn_id=None,
            task_id="linear-execute-pyn-1",
            initiated_by="linear-work-item-controller",
            request_id="pyn-1-lease",
        )
    )
    transition = await get_work_item_transition_by_request("pyn-1-lease")
    assert transition is not None
    await resolve_work_item_transition(
        transition=transition,
        execution_status=WorkItemExecutionStatus.IN_PROGRESS,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
        issue={**issue, "state": {"id": "state-progress", "name": "In Progress"}},
    )
    return original, destination


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
    prepare_workspace_issue = AsyncMock(return_value=({"id": "issue-1"}, board))
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
                    "updatedAt": datetime.now(UTC).isoformat(),
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


async def test_managed_done_lifecycle_completes_reviewed_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.complete_reviewed_work_item",
        complete,
    )
    await process_linear_webhook_lifecycle(
        WebhookLifecycleDelivery(
            identity=ExternalDeliveryIdentity(
                provider=ExternalProvider("linear"),
                route=ExternalRoute("project"),
                delivery_id=ExternalDeliveryId(_DELIVERY_ID),
            ),
            conversation_id=ConversationId("conversation-1"),
            subject_id="issue-1",
            workspace=GroupFolder("project"),
            context={
                "linear_state_id": "state-done",
                "linear_managed_done_state_id": "state-done",
            },
        )
    )

    complete.assert_awaited_once_with("project", "issue-1", _DELIVERY_ID)


@pytest.mark.parametrize("terminal_state_id", ["state-duplicate", "state-custom-canceled"])
async def test_non_managed_terminal_lifecycle_does_not_complete_reviewed_execution(
    monkeypatch: pytest.MonkeyPatch,
    terminal_state_id: str,
) -> None:
    complete = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.complete_reviewed_work_item",
        complete,
    )
    await process_linear_webhook_lifecycle(
        WebhookLifecycleDelivery(
            identity=ExternalDeliveryIdentity(
                provider=ExternalProvider("linear"),
                route=ExternalRoute("project"),
                delivery_id=ExternalDeliveryId(_DELIVERY_ID),
            ),
            conversation_id=ConversationId("conversation-1"),
            subject_id="issue-1",
            workspace=GroupFolder("project"),
            context={
                "linear_state_id": terminal_state_id,
                "linear_managed_done_state_id": "state-done",
            },
        )
    )

    complete.assert_not_awaited()


async def test_human_approved_issue_waits_for_periodic_controller_admission(
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
                "title": "Authorized outcome",
                "state": {"id": "state-approved", "name": "Human Approved"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": "state-approved"}}, board)),
    )

    processed = await process_linear_webhook_event(event)

    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is None


async def test_human_move_directly_to_in_progress_acquires_lease_in_place(
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
                "title": "Authorized outcome",
                "state": {"id": "state-progress", "name": "In Progress"},
            },
            updated_from={"stateId": "state-awaiting-plan"},
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    acquire_started = AsyncMock(
        return_value=_LeaseResult(status=WorkItemExecutionStatus.IN_PROGRESS)
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": "state-progress"}}, board)),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.acquire_human_started_work_item_lease",
        acquire_started,
    )

    processed = await process_linear_webhook_event(event)

    request = acquire_started.await_args.args[1]
    assert request.workspace == "project"
    assert request.issue_id == "issue-1"
    assert request.initiated_by == (f"linear-webhook:{_DELIVERY_ID}:user:user-1")
    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is None


@pytest.mark.parametrize(
    ("actor_type", "updated_from"),
    [
        ("integration", {"stateId": "state-awaiting-plan"}),
        ("user", {"title": "Old title"}),
    ],
)
async def test_unproven_in_progress_update_is_suppressed_without_authorizing_work(
    monkeypatch: pytest.MonkeyPatch,
    actor_type: str,
    updated_from: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    payload = _payload(
        now=now,
        event_type="Issue",
        action="update",
        data={
            "id": "issue-1",
            "identifier": "PYN-1",
            "title": "Unproven outcome",
            "state": {"id": "state-progress", "name": "In Progress"},
        },
        updated_from=updated_from,
    )
    payload["actor"] = {"id": "actor-1", "type": actor_type, "name": "Actor"}
    raw_body, headers = _signed_request(payload)
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    acquire_started = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": "state-progress"}}, board)),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.acquire_human_started_work_item_lease",
        acquire_started,
    )

    processed = await process_linear_webhook_event(event)

    acquire_started.assert_not_awaited()
    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is None


@pytest.mark.parametrize(
    ("state_id", "state_name"),
    [
        ("state-ready", "Ready for Planning"),
        ("state-awaiting-plan", "Awaiting Plan Approval"),
    ],
)
async def test_planning_issue_updates_do_not_race_the_temporal_controller(
    monkeypatch: pytest.MonkeyPatch,
    state_id: str,
    state_name: str,
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
                "title": "Plan durable recovery",
                "state": {"id": state_id, "name": state_name},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": state_id}}, board)),
    )

    processed = await process_linear_webhook_event(event)

    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is None


def test_plugin_route_requires_a_linear_enabled_discord_root() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={"webhook_routes": [{"name": "project", "workspace": "project"}]}
            )
        },
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"project": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear", public_source=False)},
    )
    with (
        patch(
            "pynchy.plugins.integrations.linear_webhooks.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=settings,
        ),
    ):
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]
        validate = route.validate_workspace
        assert validate is not None
        assert route.process_event is not None
        assert validate(_WebhookDeps().workspace) is None
        assert "Discord guild-channel" in validate(
            WorkspaceProfile(
                jid="slack:project",
                name="Project",
                folder="project",
                trigger="@Pynchy",
            )
        )
        assert "workspace root" in validate(
            WorkspaceProfile(
                jid="discord:channel:child",
                name="Child",
                folder="project__thread_child",
                trigger="@Pynchy",
            )
        )
        assert route.routes_conversations is True
        assert route.public_source is False
        assert route.prepare_event is not None


def test_plugin_route_preserves_public_linear_source_taint() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={"webhook_routes": [{"name": "project", "workspace": "project"}]}
            )
        },
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"project": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear", public_source=True)},
    )
    with (
        patch(
            "pynchy.plugins.integrations.linear_webhooks.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=settings,
        ),
    ):
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]

    assert route.public_source is True


def test_project_routed_route_declares_semantic_candidates_before_provider_boot() -> None:
    settings = make_settings(
        plugins={"linear": PluginConfig(options={"webhook_routes": [{"name": "managed-boards"}]})},
        profiles={
            "category": ProfileConfig(tools=["linear"]),
            "fam": ProfileConfig(tools=["linear"], repo="crypdick/fam"),
            "pynchy-dev": ProfileConfig(
                tools=["linear"],
                repo="crypdick/pynchy",
                execution_mode="host",
                cwd="/srv/pynchy",
                is_admin=True,
            ),
        },
        workspaces={
            "relationships": WorkspaceConfig(
                profiles=["category"],
                scopes=[{"workspace": "fam", "profiles": ["fam"]}],
            ),
            "admin": WorkspaceConfig(
                profiles=["category"],
                scopes=[{"workspace": "pynchy-dev", "profiles": ["pynchy-dev"]}],
            ),
        },
        tools={"linear": LinearTool(type="linear", public_source=False)},
    )
    with (
        patch(
            "pynchy.plugins.integrations.linear_webhooks.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=settings,
        ),
    ):
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]

    assert route.workspace is None
    assert {"fam", "pynchy-dev"} <= set(route.candidate_workspaces)
    assert route.allow_admin_workspaces is True


def test_each_route_uses_its_named_account_trust() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={
                    "webhook_routes": [
                        {
                            "name": "public",
                            "workspace": "public-project",
                            "tool": "linear_public",
                        },
                        {
                            "name": "synapse",
                            "workspace": "synapse-project",
                            "tool": "linear_synapse",
                        },
                    ]
                }
            )
        },
        profiles={
            "public": ProfileConfig(tools=["linear_public"]),
            "synapse": ProfileConfig(tools=["linear_synapse"]),
        },
        workspaces={
            "public-project": WorkspaceConfig(profiles=["public"]),
            "synapse-project": WorkspaceConfig(profiles=["synapse"]),
        },
        tools={
            "linear_public": LinearTool(type="linear", public_source=True),
            "linear_synapse": LinearTool(type="linear", public_source=False),
        },
    )
    with patch(
        "pynchy.plugins.integrations.linear_webhooks.get_settings",
        return_value=settings,
    ):
        routes = LinearMcpPlugin().pynchy_webhook_routes()

    assert [(route.name, route.public_source) for route in routes] == [
        ("public", True),
        ("synapse", False),
    ]


def test_route_rejects_a_workspace_bound_to_another_linear_account() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={
                    "webhook_routes": [
                        {
                            "name": "synapse",
                            "workspace": "project",
                            "tool": "linear_synapse",
                        }
                    ]
                }
            )
        },
        profiles={"public": ProfileConfig(tools=["linear_public"])},
        workspaces={"project": WorkspaceConfig(profiles=["public"])},
        tools={
            "linear_public": LinearTool(type="linear", public_source=True),
            "linear_synapse": LinearTool(type="linear", public_source=False),
        },
    )
    with (
        patch(
            "pynchy.plugins.integrations.linear_webhooks.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=settings,
        ),
    ):
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]
        validate = route.validate_workspace
        assert validate is not None
        error = validate(_WebhookDeps().workspace)

    assert error is not None
    assert "linear_synapse" in error


def test_non_issue_or_comment_delivery_remains_durably_ignorable() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Project",
            data={"id": "project-1", "name": "Project"},
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is None
    assert event.ignored_reason == "event_type_is_not_configured"


def test_invalid_signature_and_stale_timestamp_fail_before_parsing() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now))
    bad_headers = {**headers, "Linear-Signature": "0" * 64}

    with pytest.raises(WebhookAuthenticationError, match="signature"):
        parse_linear_webhook(raw_body, bad_headers, _SIGNING_KEY, now, config=_config())

    with pytest.raises(WebhookAuthenticationError, match="replay window"):
        parse_linear_webhook(
            raw_body,
            headers,
            _SIGNING_KEY,
            now + timedelta(minutes=2),
            config=_config(),
        )


async def test_signed_delivery_bypasses_bearer_and_enters_one_issue_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        now = datetime.now(UTC)
        first_status, first = await _post_linear_event(client, _payload(now=now))
        second_status, second = await _post_linear_event(client, _payload(now=now))
    finally:
        await client.close()

    assert first_status == second_status == 200
    assert first == {"status": "accepted", "duplicate": False}
    assert second == {"status": "accepted", "duplicate": True}
    assert not await get_all_tasks()
    assert not deps.dispatched
    assert len(deps.ingested) == 1
    message = deps.ingested[0]
    assert "EXTERNAL_UNTRUSTED_CONTENT" not in message.content
    assert "A new comment was posted" in message.content
    assert "Issue: PYN-1" in message.content
    assert "Comment:\nplease review this" in message.content
    assert message.metadata["authenticated_external_route"] is True
    assert message.metadata["public_source_input"] is False
    assert message.metadata["external_provider"] == "linear"
    receipt = await get_webhook_receipt("linear", "project", _DELIVERY_ID)
    assert receipt is not None
    assert receipt.disposition == "routed"
    assert receipt.task_id is None
    conversation = await get_conversation(message.metadata["conversation_id"])
    binding = await get_conversation_control_binding(message.metadata["conversation_id"])
    assert conversation is not None
    assert binding is not None
    assert conversation.subject.namespace == "linear:org-1:issue"
    assert binding.thread_jid == message.chat_jid
    assert binding.title == "[PYN-1] Linear issue"


async def test_public_source_linear_route_keeps_comment_context_fenced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    route = replace(_route(), public_source=True)
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await _post_linear_event(client, _payload(now=datetime.now(UTC)))
    finally:
        await client.close()

    message = deps.ingested[0]
    assert "EXTERNAL_UNTRUSTED_CONTENT" in message.content
    assert message.metadata["public_source_input"] is True


async def test_route_preparation_can_ignore_an_off_board_issue_before_thread_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reject_delivery = True

    async def reject(  # noqa: RUF029, RUF100 - webhook preparers are asynchronous callbacks.
        event: WebhookEvent,
    ) -> WebhookEvent:
        if not reject_delivery:
            return event
        return replace(
            event,
            instructions=None,
            external_context=None,
            ignored_reason="issue_is_not_on_workspace_board",
            conversation=None,
        )

    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    route = replace(_route(), prepare_event=reject)
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    event_payload = _payload(now=datetime.now(UTC))
    await client.start_server()
    try:
        status, body = await _post_linear_event(client, event_payload)
        reject_delivery = False
        duplicate_status, duplicate = await _post_linear_event(client, event_payload)
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "ignored", "duplicate": False}
    assert duplicate_status == 200
    assert duplicate == {"status": "ignored", "duplicate": True}
    assert not deps.ingested
    assert not deps.channel.created


@asynccontextmanager
async def _linear_client_context() -> AsyncIterator[object]:
    yield object()


async def test_route_preparation_verifies_board_membership_before_dispatch() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now))
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    prepare = _route().prepare_event
    assert prepare is None

    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={"webhook_routes": [{"name": "project", "workspace": "project"}]}
            )
        },
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"project": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear", public_source=False)},
    )
    with (
        patch(
            "pynchy.plugins.integrations.linear_webhooks.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_webhooks.linear_client",
            return_value=_linear_client_context(),
        ) as client_factory,
        patch(
            "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
            new_callable=AsyncMock,
            return_value=({"id": "issue-1"}, _workspace_board()),
        ) as workspace_issue,
    ):
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]
        assert route.prepare_event is not None
        prepared = await route.prepare_event(event)
        assert prepared.conversation is not None
        assert prepared.conversation.workspace == "project"
        assert prepared.conversation.public_source is False

    workspace_issue.assert_awaited_once_with(ANY, "project", "issue-1")
    client_factory.assert_called_once_with(account_name="linear")


async def test_project_route_selects_issue_workspace_instead_of_ingress_scope() -> None:
    now = datetime.now(UTC)
    config = LinearWebhookRouteConfig(name="all-boards")
    raw_body, headers = _signed_request(_payload(now=now))
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=config)
    client = AsyncMock()
    client.get_issue.return_value = {
        "id": "issue-1",
        "project": {"id": "project-fam"},
    }

    @asynccontextmanager
    async def client_context() -> AsyncIterator[object]:
        yield client

    settings = make_settings(
        profiles={"fam": ProfileConfig(tools=["linear"])},
        workspaces={"fam": WorkspaceConfig(profiles=["fam"])},
        tools={"linear": LinearTool(type="linear", public_source=False)},
    )
    with (
        patch(
            "pynchy.plugins.integrations.linear_webhooks.linear_client",
            return_value=client_context(),
        ),
        patch(
            "pynchy.plugins.integrations.linear_webhooks.workspace_for_linear_project",
            return_value="fam",
        ),
        patch(
            "pynchy.plugins.integrations.linear_webhooks.get_settings",
            return_value=settings,
        ),
        patch(
            "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
            new_callable=AsyncMock,
            return_value=({"id": "issue-1"}, _workspace_board()),
        ),
    ):
        prepared = await prepare_linear_webhook_event(
            event,
            config=config,
            public_source=False,
        )

    assert prepared.conversation is not None
    assert prepared.conversation.workspace == "fam"
    assert prepared.conversation.public_source is False


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LinearWorkspaceIssueError("wrong board"), "ignored"),
        (LinearError("provider unavailable"), "error"),
    ],
)
async def test_route_preparation_distinguishes_off_board_from_provider_failure(
    error: Exception,
    expected: str,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now))
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    with (
        patch(
            "pynchy.plugins.integrations.linear_webhooks.linear_client",
            return_value=_linear_client_context(),
        ),
        patch(
            "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
            new_callable=AsyncMock,
            side_effect=error,
        ),
    ):
        if expected == "error":
            with pytest.raises(WebhookProcessingError, match=str(error)):
                await prepare_linear_webhook_event(event, config=_config())
            return
        prepared = await prepare_linear_webhook_event(event, config=_config())

    assert prepared.instructions is None
    assert prepared.conversation is None
    assert prepared.ignored_reason == "issue_is_not_on_workspace_board"


async def test_same_issue_fifo_reuses_session_while_other_issue_runs_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        now = datetime.now(UTC)
        await _post_linear_event(client, _payload(now=now))
        first = deps.ingested[0]
        conversation_id = first.metadata["conversation_id"]
        conversation = await set_conversation_session(
            conversation_id,
            SessionId("linear-session-1"),
        )

        second_payload = _payload(
            now=now + timedelta(seconds=1),
            data={
                "id": "comment-2",
                "issueId": "issue-1",
                "body": "second event",
            },
        )
        await _post_linear_event(
            client,
            second_payload,
            delivery_id=_SECOND_DELIVERY_ID,
        )
        assert len(deps.ingested) == 1

        await complete_turn_with_cursor(
            _CursorDeps(),
            first.chat_jid,
            first.timestamp,
            "linear-turn-1",
            conversation_claim_id=first.metadata["conversation_claim_id"],
        )
        assert len(deps.ingested) == 2
        second = deps.ingested[1]
        assert second.metadata["conversation_id"] == conversation.id
        assert second.metadata["conversation_claim_id"] != first.metadata["conversation_claim_id"]
        assert deps.bound_sessions[-1] == (
            routed_conversation_folder(conversation.workspace, conversation.id),
            SessionId("linear-session-1"),
        )

        other_payload = _payload(
            now=now + timedelta(seconds=2),
            event_type="Issue",
            action="update",
            data={
                "id": "issue-2",
                "identifier": "PYN-2",
                "title": "Independent issue",
                "state": {"id": "state-1", "name": "Agent Proposed"},
            },
            url="https://linear.app/acme/issue/PYN-2",
        )
        await _post_linear_event(
            client,
            other_payload,
            delivery_id=_THIRD_DELIVERY_ID,
        )
    finally:
        await client.close()

    assert len(deps.ingested) == 3
    other = deps.ingested[2]
    assert other.metadata["conversation_id"] != conversation.id
    assert other.chat_jid != first.chat_jid


async def test_deleted_discord_binding_is_replaced_without_resetting_issue_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        now = datetime.now(UTC)
        await _post_linear_event(client, _payload(now=now))
        first = deps.ingested[0]
        conversation_id = first.metadata["conversation_id"]
        await set_conversation_session(conversation_id, SessionId("linear-session-1"))
        await complete_turn_with_cursor(
            _CursorDeps(),
            first.chat_jid,
            first.timestamp,
            "linear-turn-before-delete",
            conversation_claim_id=first.metadata["conversation_claim_id"],
        )
        first_binding = await get_conversation_control_binding(conversation_id)
        assert first_binding is not None
        del deps.channel.threads[deps.workspace.jid, first_binding.title]

        await _post_linear_event(
            client,
            _payload(
                now=now + timedelta(seconds=1),
                data={
                    "id": "comment-2",
                    "issueId": "issue-1",
                    "body": "wake replacement",
                },
            ),
            delivery_id=_SECOND_DELIVERY_ID,
        )
    finally:
        await client.close()

    replacement = await get_conversation_control_binding(conversation_id)
    conversation = await get_conversation(conversation_id)
    assert replacement is not None
    assert conversation is not None
    assert replacement.thread_jid != first_binding.thread_jid
    assert replacement.title == first_binding.title
    assert conversation.session_id == SessionId("linear-session-1")


async def test_ignored_delivery_records_receipt_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        payload = _payload(
            now=datetime.now(UTC),
            event_type="Project",
            data={"id": "project-1", "name": "Project"},
        )
        status, body = await _post_linear_event(client, payload)
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "ignored", "duplicate": False}
    assert not deps.dispatched
    assert not await get_all_tasks()


async def test_agent_proposed_issue_creation_is_ignored_until_later_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "done": {"id": "state-done"},
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-in-progress"},
        },
    )
    prepare_client = Mock(side_effect=lambda **_kwargs: _linear_client_context())
    prepare_workspace_issue = AsyncMock(return_value=({"id": "issue-1"}, board))
    process_client = Mock(side_effect=lambda **_kwargs: _linear_client_context())
    process_workspace_issue = AsyncMock(return_value=({"state": {"id": "state-triage"}}, board))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        prepare_client,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        prepare_workspace_issue,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        process_client,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        process_workspace_issue,
    )
    route = replace(
        _route(),
        prepare_event=partial(
            prepare_linear_webhook_event,
            config=_config(),
            public_source=False,
        ),
        process_event=process_linear_webhook_event,
    )
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    now = datetime.now(UTC)
    created_payload = _payload(
        now=now,
        event_type="Issue",
        action="create",
        data={
            "id": "issue-1",
            "identifier": "PYN-1",
            "title": "Created issue",
            "state": {
                "id": "state-agent-proposed",
                "name": "Agent Proposed",
                "type": "backlog",
            },
        },
        url="https://linear.app/acme/issue/PYN-1",
    )
    update_payload = _payload(
        now=now + timedelta(seconds=1),
        event_type="Issue",
        action="update",
        data={
            "id": "issue-1",
            "identifier": "PYN-1",
            "title": "Created issue",
            "state": {
                "id": "state-triage",
                "name": "Triage",
                "type": "backlog",
            },
        },
        updated_from={"state": {"id": "state-agent-proposed"}},
        url="https://linear.app/acme/issue/PYN-1",
    )
    await client.start_server()
    try:
        create_status, created = await _post_linear_event(client, created_payload)
        creation_receipt = await get_webhook_receipt("linear", "project", _DELIVERY_ID)

        assert create_status == 200
        assert created == {"status": "ignored", "duplicate": False}
        assert creation_receipt is not None
        assert creation_receipt.disposition == "ignored"
        assert creation_receipt.ignored_reason == "issue_creation_does_not_authorize_work"
        assert not deps.ingested
        assert not deps.dispatched
        assert not deps.channel.created
        assert not await get_all_tasks()
        prepare_client.assert_not_called()
        prepare_workspace_issue.assert_not_awaited()
        process_client.assert_not_called()
        process_workspace_issue.assert_not_awaited()

        update_status, updated = await _post_linear_event(
            client,
            update_payload,
            delivery_id=_SECOND_DELIVERY_ID,
        )
        prepare_client.assert_called_once_with(account_name="linear")
        prepare_workspace_issue.assert_awaited_once_with(ANY, "project", "issue-1")
        process_client.assert_called_once_with(workspace="project")
        process_workspace_issue.assert_awaited_once_with(ANY, "project", "issue-1")
        replay_status, replay = await _post_linear_event(
            client,
            update_payload,
            delivery_id=_SECOND_DELIVERY_ID,
        )
        process_client.assert_called_once_with(workspace="project")
        process_workspace_issue.assert_awaited_once_with(ANY, "project", "issue-1")
    finally:
        await client.close()

    update_receipt = await get_webhook_receipt("linear", "project", _SECOND_DELIVERY_ID)
    assert update_status == replay_status == 200
    assert updated == {"status": "accepted", "duplicate": False}
    assert replay == {"status": "accepted", "duplicate": True}
    assert update_receipt is not None
    assert update_receipt.disposition == "routed"
    assert len(deps.ingested) == 1
    assert len(deps.channel.created) == 1


async def test_in_progress_issue_creation_enters_one_routed_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    issue_payload = _payload(
        now=datetime.now(UTC),
        event_type="Issue",
        action="create",
        data={
            "id": "issue-1",
            "identifier": "PYN-1",
            "title": "Ready work",
            "state": {
                "id": "state-in-progress",
                "name": "In Progress",
                "type": "started",
            },
        },
        url="https://linear.app/acme/issue/PYN-1",
    )
    await client.start_server()
    try:
        first_status, first = await _post_linear_event(client, issue_payload)
        replay_status, replay = await _post_linear_event(client, issue_payload)
    finally:
        await client.close()

    receipt = await get_webhook_receipt("linear", "project", _DELIVERY_ID)
    assert first_status == replay_status == 200
    assert first == {"status": "accepted", "duplicate": False}
    assert replay == {"status": "accepted", "duplicate": True}
    assert receipt is not None
    assert receipt.disposition == "routed"
    assert len(deps.ingested) == 1
    assert len(deps.channel.created) == 1
    assert not deps.dispatched
    assert not await get_all_tasks()


def test_route_refuses_admin_workspace() -> None:
    with pytest.raises(WebhookConfigurationError, match="cannot target admin"):
        create_http_app(
            _WebhookDeps(admin=True),
            runtime=_public_runtime(),
            webhook_routes=(_route(),),
        )


def test_route_requires_its_signing_secret_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)

    with pytest.raises(WebhookConfigurationError, match="requires environment variable"):
        create_http_app(
            _WebhookDeps(),
            runtime=_public_runtime(),
            webhook_routes=(_route(),),
        )
