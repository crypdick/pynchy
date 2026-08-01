"""Behavioral coverage for authenticated Linear issue-conversation admission."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from linear_webhook_test_support import (
    DELIVERY_ID as _DELIVERY_ID,
)
from linear_webhook_test_support import (
    SIGNING_KEY as _SIGNING_KEY,
)
from linear_webhook_test_support import (
    payload as _payload,
)
from linear_webhook_test_support import (
    route_config as _config,
)
from linear_webhook_test_support import (
    signed_request as _signed_request,
)

from pynchy.conversation.models import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.host.orchestrator.webhook_event_payloads import (
    webhook_event_from_payload,
    webhook_event_payload,
)
from pynchy.identifiers import (
    GroupFolder,
)
from pynchy.plugins.api import (
    WebhookConversation,
    WebhookEvent,
    WebhookProcessingError,
)
from pynchy.plugins.integrations.linear_webhook_effects import (
    process_linear_webhook_event,
)
from pynchy.plugins.integrations.linear_webhook_prompts import LinearWebhookPrompts
from pynchy.plugins.integrations.linear_webhooks import (
    parse_linear_webhook,
    prepare_linear_webhook_event,
)
from pynchy.state import (
    apply_conversation_control_state,
    get_conversation,
    resolve_conversation,
)
from tests.linear_webhooks_support import (
    _linear_client_context,
    _workspace_board,
)

pytest_plugins = ("tests.linear_webhooks_support",)


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


@pytest.mark.parametrize("action", ["create", "update", "remove"])
def test_every_comment_change_maps_to_concise_issue_conversation(action: str) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now, action=action))

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.subject_id == "issue-1"
    assert event.instructions is not None
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


def test_webhook_parser_uses_injected_synthetic_prompt_content() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now))
    prompts = LinearWebhookPrompts(issue="issue fixture", comment="comment fixture")

    event = parse_linear_webhook(
        raw_body,
        headers,
        _SIGNING_KEY,
        now,
        config=_config(),
        prompts=prompts,
    )

    assert event.instructions == prompts.comment


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
        "linear_controller_workspace": "project",
    }
    assert prepared.conversation is not None
    assert prepared.conversation.workspace == "project"
    workspace_issue.assert_awaited_once()


async def test_preparation_ignores_an_untyped_current_state(
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
                "title": "Untyped current state",
                "state": {"id": "state-done", "name": "Done", "type": "completed"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhooks.workspace_issue",
        AsyncMock(
            return_value=(
                {"id": "issue-1", "state": {"id": "", "type": "started"}},
                _workspace_board(),
            )
        ),
    )

    prepared = await prepare_linear_webhook_event(event, config=_config())

    assert prepared.conversation is not None
    assert prepared.lifecycle is not None


async def test_preparation_rejects_typed_current_state_without_revision(
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
                "title": "Missing revision",
                "state": {"id": "state-done", "name": "Done", "type": "completed"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
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
                    "state": {"id": "state-started", "type": "started"},
                },
                _workspace_board(),
            )
        ),
    )

    with pytest.raises(WebhookProcessingError, match="lacks updatedAt"):
        await prepare_linear_webhook_event(event, config=_config())


async def test_preparation_uses_current_terminal_state_over_stale_nonterminal_callback(
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
                "title": "Stale callback",
                "state": {"id": "state-started", "name": "In Progress", "type": "started"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

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
                    "updatedAt": "2026-07-27T00:00:01+00:00",
                    "state": {
                        "id": "state-canceled",
                        "name": "Canceled",
                        "type": "canceled",
                    },
                },
                _workspace_board(),
            )
        ),
    )

    prepared = await prepare_linear_webhook_event(event, config=_config())

    assert prepared.instructions is None
    assert prepared.external_context is None
    assert prepared.lifecycle is not None
    assert prepared.lifecycle.context == {
        "linear_state_id": "state-canceled",
        "linear_managed_done_state_id": "state-done",
        "linear_controller_workspace": "project",
    }
    assert prepared.conversation is not None
    assert prepared.conversation.control_closed is True


async def test_preparation_ignores_stale_terminal_callback_after_current_state_reopens(
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
                "title": "Stale terminal callback",
                "state": {"id": "state-done", "name": "Done", "type": "completed"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    conversation = await resolve_conversation(event.conversation.subject, GroupFolder("project"))
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision=None,
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

    assert prepared.instructions is None
    assert prepared.external_context is None
    assert prepared.lifecycle is None
    assert prepared.conversation is not None
    assert prepared.conversation.control_closed is False
    assert prepared.ignored_reason == "stale_terminal_issue_state"

    processed = await process_linear_webhook_event(prepared)
    assert processed.conversation is not None
    reopened = await get_conversation(conversation.id)
    assert reopened is not None
    assert reopened.control_closed is False


async def test_preparation_marks_current_nonterminal_issue_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now))
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

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
                    "updatedAt": "2026-07-27T00:00:03+00:00",
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

    assert prepared.conversation is not None
    assert prepared.conversation.control_closed is False
