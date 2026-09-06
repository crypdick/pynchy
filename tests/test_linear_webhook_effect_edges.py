"""Public Linear webhook effect behavior for ignored and failed callbacks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import configure_linear_accounts_for, make_settings
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
    ConversationClaimId,
    ConversationId,
    ConversationLifecycleFence,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import GroupFolder
from pynchy.plugins.api import (
    WebhookConversation,
    WebhookEvent,
    WebhookLifecycle,
    WebhookLifecycleDelivery,
    WebhookProcessingError,
)
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_webhook_effects import (
    LinearWebhookEffectsRuntime,
    configure_linear_webhook_effects_runtime,
    process_linear_webhook_event,
    process_linear_webhook_lifecycle,
)
from pynchy.plugins.integrations.linear_webhooks import parse_linear_webhook
from pynchy.state import resolve_conversation
from pynchy.work_items.api import WorkItemExecutionStatus
from tests.linear_webhooks_support import _LeaseResult, _linear_client_context

pytest_plugins = ("tests.linear_webhooks_support",)


@dataclass(frozen=True)
class _Execution:
    id: str
    status: WorkItemExecutionStatus


def _conversation(*, workspace: str | None = "project", closed: bool | None = None):
    return WebhookConversation(
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:org-1:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        control_title="[PYN-1] Webhook issue",
        workspace=workspace,
        control_closed=closed,
    )


def _event(*, conversation: WebhookConversation, lifecycle: WebhookLifecycle | None = None):
    return WebhookEvent(
        delivery_id="delivery-1",
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at=datetime.now(UTC).isoformat(),
        instructions=None if lifecycle is not None else "Handle this issue.",
        external_context=None if lifecycle is not None else "Issue update",
        conversation=conversation,
        lifecycle=lifecycle,
    )


def _delivery(*, context: dict[str, object] | None) -> WebhookLifecycleDelivery:
    return WebhookLifecycleDelivery(
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId("delivery-1"),
        ),
        conversation_id=ConversationId("conversation-1"),
        subject_id="issue-1",
        workspace=GroupFolder("project"),
        context=context,
    )


async def test_lifecycle_event_is_not_reprocessed_as_an_agent_event():
    event = _event(
        conversation=_conversation(closed=True),
        lifecycle=WebhookLifecycle({"linear_state_id": "state-done"}),
    )

    assert await process_linear_webhook_event(event) is event


async def test_actionable_event_requires_a_resolved_workspace():
    event = _event(conversation=_conversation(workspace=None))

    with pytest.raises(WebhookProcessingError, match="no resolved workspace"):
        await process_linear_webhook_event(event)


async def test_closed_control_reopen_requires_a_provider_revision():
    event = _event(conversation=_conversation(closed=False))

    with pytest.raises(WebhookProcessingError, match="lacks updatedAt"):
        await process_linear_webhook_event(event)


@pytest.mark.parametrize(
    "context",
    [
        None,
        {"linear_state_id": "state-done"},
    ],
)
async def test_lifecycle_callback_ignores_incomplete_provider_context(
    context: dict[str, object] | None,
):
    await process_linear_webhook_lifecycle(_delivery(context=context))


async def test_lifecycle_callback_ignores_empty_controller_workspace():
    await process_linear_webhook_lifecycle(
        _delivery(
            context={
                "linear_state_id": "state-cancelled",
                "linear_managed_done_state_id": "state-done",
                "linear_controller_workspace": "",
            }
        )
    )


async def test_lifecycle_callback_requires_composed_runtime_for_nonterminal_state(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("pynchy.plugins.integrations.linear_webhook_effects._runtime", None)

    with pytest.raises(RuntimeError, match="effects runtime has not been configured"):
        await process_linear_webhook_lifecycle(
            _delivery(
                context={
                    "linear_state_id": "state-cancelled",
                    "linear_managed_done_state_id": "state-done",
                }
            )
        )


def _provider_event(*, state_id: str, state_name: str, updated_from: dict[str, object]):
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Provider state update",
                "state": {"id": state_id, "name": state_name},
            },
            updated_from=updated_from,
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    return replace(event, conversation=replace(event.conversation, workspace="project"))


async def test_human_start_with_unactive_lease_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
):
    event = _provider_event(
        state_id="state-progress",
        state_name="In Progress",
        updated_from={"stateId": "state-awaiting-plan"},
    )
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
        AsyncMock(return_value=({"state": {"id": "state-progress"}}, board)),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.acquire_human_started_work_item_lease",
        AsyncMock(return_value=_LeaseResult(status=WorkItemExecutionStatus.CLAIMING)),
    )

    with pytest.raises(WebhookProcessingError, match="did not become active"):
        await process_linear_webhook_event(event)


async def test_reconciliation_failure_does_not_break_human_approved_admission(
    monkeypatch: pytest.MonkeyPatch,
):
    event = _provider_event(
        state_id="state-approved",
        state_name="Human Approved",
        updated_from={"stateId": "state-awaiting-plan"},
    )
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
    reconcile = AsyncMock(side_effect=RuntimeError("poll unavailable"))
    configure_linear_accounts_for(make_settings(), start_work_item_reconciliation=reconcile)

    processed = await process_linear_webhook_event(event)

    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    reconcile.assert_awaited_once_with()


async def test_provider_error_is_reported_as_webhook_processing_error(
    monkeypatch: pytest.MonkeyPatch,
):
    event = _provider_event(
        state_id="state-approved",
        state_name="Human Approved",
        updated_from={"stateId": "state-awaiting-plan"},
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(side_effect=ValueError("malformed provider state")),
    )

    with pytest.raises(WebhookProcessingError, match="malformed provider state"):
        await process_linear_webhook_event(event)


async def test_stale_control_state_suppresses_controller_processing():
    event = _event(
        conversation=replace(
            _conversation(),
            control_closed=False,
            control_state_revision="2026-07-31T00:00:02+00:00",
        )
    )
    control_matches = AsyncMock(return_value=False)
    configure_linear_webhook_effects_runtime(
        LinearWebhookEffectsRuntime(
            resolve_conversation=resolve_conversation,
            control_state_matches=control_matches,
            apply_control_state=AsyncMock(return_value=True),
            get_execution_for_issue=AsyncMock(),
            cancel_execution=AsyncMock(),
            cancel_execution_if_lifecycle_current=AsyncMock(),
            get_active_execution=AsyncMock(),
            start_work_item_reconciliation=AsyncMock(),
        )
    )

    processed = await process_linear_webhook_event(event)

    assert processed.ignored_reason == "stale_linear_control_state"
    assert processed.conversation is None
    control_matches.assert_awaited_once()


async def test_terminal_lifecycle_with_fence_cancels_current_execution():
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId("delivery-1"),
    )
    fence = ConversationLifecycleFence(
        conversation_id=ConversationId("conversation-1"),
        identity=identity,
        claim_id=ConversationClaimId("claim-1"),
        control_state_revision="2026-07-31T00:00:01+00:00",
    )
    cancel = AsyncMock()
    configure_linear_webhook_effects_runtime(
        LinearWebhookEffectsRuntime(
            resolve_conversation=AsyncMock(),
            control_state_matches=AsyncMock(),
            apply_control_state=AsyncMock(),
            get_execution_for_issue=AsyncMock(
                return_value=_Execution("execution-1", WorkItemExecutionStatus.IN_PROGRESS)
            ),
            cancel_execution=AsyncMock(),
            cancel_execution_if_lifecycle_current=cancel,
            get_active_execution=AsyncMock(),
            start_work_item_reconciliation=AsyncMock(),
        )
    )

    await process_linear_webhook_lifecycle(
        WebhookLifecycleDelivery(
            identity=identity,
            conversation_id=ConversationId("conversation-1"),
            subject_id="issue-1",
            workspace=GroupFolder("project"),
            context={
                "linear_state_id": "state-cancelled",
                "linear_managed_done_state_id": "state-done",
            },
            lifecycle_fence=fence,
        )
    )

    cancel.assert_awaited_once()
    assert cancel.await_args.kwargs["lifecycle_fence"] == fence


async def test_cancelled_execution_keeps_blocked_issue_controller_owned(
    monkeypatch: pytest.MonkeyPatch,
):
    event = _provider_event(
        state_id="state-blocked",
        state_name="Blocked",
        updated_from={"stateId": "state-approved"},
    )
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
            "blocked": {"id": "state-blocked"},
        },
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": "state-blocked"}}, board)),
    )
    configure_linear_webhook_effects_runtime(
        LinearWebhookEffectsRuntime(
            resolve_conversation=resolve_conversation,
            control_state_matches=AsyncMock(return_value=True),
            apply_control_state=AsyncMock(return_value=True),
            get_execution_for_issue=AsyncMock(
                return_value=_Execution("execution-1", WorkItemExecutionStatus.CANCELLED)
            ),
            cancel_execution=AsyncMock(),
            cancel_execution_if_lifecycle_current=AsyncMock(),
            get_active_execution=AsyncMock(return_value=None),
            start_work_item_reconciliation=AsyncMock(),
        )
    )

    processed = await process_linear_webhook_event(event)

    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
