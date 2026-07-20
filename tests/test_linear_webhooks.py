"""Behavioral coverage for authenticated Linear issue-conversation admission."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

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
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.messaging.cursor import complete_turn_with_cursor
from pynchy.plugins.integrations.linear_webhooks import (
    linear_webhook_routes,
    parse_linear_webhook,
)
from pynchy.plugins.webhooks import WebhookAuthenticationError, WebhookConfigurationError
from pynchy.state import (
    get_all_tasks,
    get_conversation,
    get_conversation_control_binding,
    get_webhook_receipt,
    init_test_database,
    set_conversation_session,
)
from pynchy.types import SessionId, WorkspaceProfile


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


@pytest.mark.parametrize("action", ["create", "update", "remove"])
def test_every_comment_change_maps_to_fenced_issue_conversation(action: str) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now, action=action))

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.subject_id == "issue-1"
    assert event.instructions is not None
    assert "linear_get_issue" in event.instructions
    assert "linear_list_todos" in event.instructions
    assert "linear_submit_plan" in event.instructions
    assert "linear_claim_work_item" in event.instructions
    assert "does not grant execution authority" in event.instructions
    assert event.external_context is not None
    assert event.external_context["action"] == action
    assert event.external_context["comment_body"] == "please review this"
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
                "state": {"id": "state-1", "name": "In Progress"},
            },
            updated_from=updated_from,
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is not None
    assert event.external_context is not None
    assert event.external_context["action"] == action
    assert event.external_context["updated_fields"] == (
        ["title"] if updated_from is not None else []
    )
    assert event.conversation is not None
    assert event.conversation.control_title == "[PYN-1] Webhook callbacks"
    assert event.conversation.control_closed is False


def test_plugin_route_requires_a_linear_enabled_discord_root() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={"webhook_routes": [{"name": "project", "workspace": "project"}]}
            )
        },
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"project": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
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
        route = linear_webhook_routes()[0]
        validate = route.validate_workspace
        assert validate is not None
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
    assert "EXTERNAL_UNTRUSTED_CONTENT" in message.content
    assert message.metadata["authenticated_external_route"] is True
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
            data={
                "id": "issue-2",
                "identifier": "PYN-2",
                "title": "Independent issue",
                "state": {"id": "state-1", "name": "Ready for Planning"},
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
