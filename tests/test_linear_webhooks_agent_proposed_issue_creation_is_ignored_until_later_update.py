"""Behavioral coverage for authenticated Linear issue-conversation admission."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
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
    webhook_route as _route,
)

from pynchy.conversation.models import (
    ConversationSubjectKey,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.identifiers import (
    GroupFolder,
)
from pynchy.plugins.api import (
    WebhookConfigurationError,
)
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_webhook_effects import (
    process_linear_webhook_event,
)
from pynchy.plugins.integrations.linear_webhooks import (
    prepare_linear_webhook_event,
)
from pynchy.state import (
    get_all_tasks,
    get_conversation_for_subject_key,
    get_webhook_receipt,
)
from tests.linear_webhooks_support import (
    _linear_client_context,
    _workspace_board,
)

pytest_plugins = ("tests.linear_webhooks_support",)


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


async def test_project_assignment_only_update_resolves_ownership_without_waking_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    await deps.persist_parent()
    prepare_workspace_issue = AsyncMock(return_value=({"id": "issue-1"}, _workspace_board()))
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        prepare_workspace_issue,
    )
    process_client = Mock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        process_client,
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
                    "title": "Moved proposal",
                    "state": {
                        "id": "state-agent-proposed",
                        "name": "Agent Proposed",
                        "type": "backlog",
                    },
                },
                updated_from={
                    "addedToProjectAt": None,
                    "projectId": "old-project",
                    "updatedAt": "old-revision",
                },
                url="https://linear.app/acme/issue/PYN-1",
            ),
        )
    finally:
        await client.close()

    receipt = await get_webhook_receipt("linear", "project", _DELIVERY_ID)
    assert status == 200
    assert body == {"status": "ignored", "duplicate": False}
    assert receipt is not None
    assert receipt.disposition == "ignored"
    assert receipt.ignored_reason == "issue_project_assignment_does_not_wake_agent"
    assert not deps.ingested
    assert not deps.dispatched
    assert not deps.channel.created
    assert not await get_all_tasks()
    prepare_workspace_issue.assert_awaited_once_with(ANY, "project", "issue-1")
    process_client.assert_not_called()
    assert (
        await get_conversation_for_subject_key(
            ConversationSubjectKey("issue-1"),
            workspace=GroupFolder("project"),
            namespace_suffix=":issue",
        )
        is not None
    )


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
