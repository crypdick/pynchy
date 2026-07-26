"""Race-safe durable correlation for Pynchy-created Linear comments."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.conversation.models import (
    ControlSurface,
    ConversationClaimId,
    ConversationControlBinding,
    ConversationDeliveryStatus,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.plugins.integrations.linear_client import (
    LinearClient,
    LinearError,
    LinearSelfEchoRecorder,
)
from pynchy.plugins.integrations.linear_comment_actions import handle_create_comment
from pynchy.plugins.integrations.linear_webhook_evidence import comment_webhook_evidence
from pynchy.state import (
    WebhookConversationRequest,
    WebhookReceipt,
    admit_conversation_delivery,
    admit_webhook_conversation,
    admit_webhook_receipt,
    begin_webhook_effect,
    claim_next_conversation_delivery,
    confirm_webhook_effect,
    get_conversation_delivery,
    get_webhook_receipt,
    init_test_database,
    mark_webhook_effect_executing,
    mark_webhook_effect_outcome_unknown,
    reconcile_webhook_effect_absent,
    recover_incomplete_webhook_effects,
    set_chat_cleared_at,
    set_conversation_control_binding,
    store_chat_metadata,
)
from pynchy.types import ChatJid, GroupFolder
from pynchy.webhook_effects import WebhookEffectId, WebhookEffectScope

_RECEIVED_AT = "2026-07-26T16:00:00+00:00"
_REVISION = "2026-07-26T16:00:01+00:00"
_ISSUE_ID = "issue-1"


class _LinearClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _scope() -> WebhookEffectScope:
    return WebhookEffectScope(
        provider="linear",
        account="linear-project",
        event_type="Comment",
        event_action="create",
        subject_id=_ISSUE_ID,
    )


def _evidence(*, revision: str = _REVISION):
    return comment_webhook_evidence(
        "linear-project",
        comment_id="comment-1",
        issue_id=_ISSUE_ID,
        revision=revision,
    )


def _receipt(route: str, delivery_id: str) -> WebhookReceipt:
    occurred_at = datetime.now(UTC).isoformat()
    return WebhookReceipt(
        provider="linear",
        route=route,
        delivery_id=delivery_id,
        workspace="project",
        event_type="Comment",
        event_action="create",
        subject_id=_ISSUE_ID,
        payload_sha256=f"payload-{route}-{delivery_id}",
        disposition="routed",
        ignored_reason=None,
        task_id=None,
        occurred_at=occurred_at,
        received_at=occurred_at,
    )


def _identity(route: str, delivery_id: str) -> ExternalDeliveryIdentity:
    return ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute(route),
        delivery_id=ExternalDeliveryId(delivery_id),
    )


def _subject() -> ConversationSubject:
    return ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:org-1:issue"),
        key=ConversationSubjectKey(_ISSUE_ID),
    )


async def _begin_effect():
    effect_id = await begin_webhook_effect(_scope())
    await mark_webhook_effect_executing(effect_id)
    return effect_id


async def test_confirmed_evidence_suppresses_every_route_without_consumption() -> None:
    effect_id = await _begin_effect()
    await confirm_webhook_effect(effect_id, _evidence())

    first = await admit_webhook_receipt(
        _receipt("project-a", "delivery-a"),
        None,
        effect_evidence=_evidence(),
    )
    second = await admit_webhook_receipt(
        _receipt("project-b", "delivery-b"),
        None,
        effect_evidence=_evidence(),
    )

    assert first.outbound_effect_suppressed is True
    assert second.outbound_effect_suppressed is True
    assert first.receipt.disposition == second.receipt.disposition == "routed"


async def test_callback_before_response_is_held_then_completed_without_a_claim() -> None:
    effect_id = await _begin_effect()
    receipt = _receipt("project", "delivery-1")
    admission = await admit_webhook_receipt(
        receipt,
        None,
        effect_evidence=_evidence(),
    )
    delivery = await admit_conversation_delivery(
        _identity("project", "delivery-1"),
        _subject(),
        GroupFolder("project"),
        payload={"prompt": "must not run"},
    )

    assert admission.outbound_effect_held is True
    assert delivery is not None
    assert delivery.delivery.status is ConversationDeliveryStatus.HELD
    assert (
        await claim_next_conversation_delivery(
            delivery.conversation.id,
            ConversationClaimId("claim-before-confirm"),
        )
        is None
    )

    resolution = await confirm_webhook_effect(effect_id, _evidence())
    stored = await get_conversation_delivery(_identity("project", "delivery-1"))

    assert len(resolution.wakeups) == 1
    assert stored is not None
    assert stored.status is ConversationDeliveryStatus.COMPLETED


async def test_confirmation_between_receipt_and_fifo_admission_cannot_use_stale_state() -> None:
    effect_id = await _begin_effect()
    receipt = _receipt("project", "delivery-admission-race")
    admission = await admit_webhook_receipt(
        receipt,
        None,
        effect_evidence=_evidence(),
    )
    assert admission.outbound_effect_held is True

    await confirm_webhook_effect(effect_id, _evidence())
    delivery = await admit_conversation_delivery(
        _identity("project", "delivery-admission-race"),
        _subject(),
        GroupFolder("project"),
        payload={"prompt": "stale in-memory admission"},
    )

    assert delivery is None


async def test_held_fifo_head_blocks_a_later_pending_delivery() -> None:
    effect_id = await _begin_effect()
    held_receipt = _receipt("project", "delivery-held")
    await admit_webhook_receipt(
        held_receipt,
        None,
        effect_evidence=_evidence(revision="2026-07-26T16:00:09+00:00"),
    )
    held = await admit_conversation_delivery(
        _identity("project", "delivery-held"),
        _subject(),
        GroupFolder("project"),
        payload={"prompt": "held callback"},
    )
    assert held is not None

    pending_receipt = _receipt("project", "delivery-pending")
    await admit_webhook_receipt(pending_receipt, None)
    pending = await admit_conversation_delivery(
        _identity("project", "delivery-pending"),
        _subject(),
        GroupFolder("project"),
        payload={"prompt": "later callback"},
    )
    assert pending is not None
    assert (
        await claim_next_conversation_delivery(
            held.conversation.id,
            ConversationClaimId("must-not-skip-held"),
        )
        is None
    )

    await confirm_webhook_effect(effect_id, _evidence())
    claimed = await claim_next_conversation_delivery(
        held.conversation.id,
        ConversationClaimId("held-released-first"),
    )

    assert claimed is not None
    assert claimed.identity.delivery_id == ExternalDeliveryId("delivery-held")


async def test_reset_retires_held_delivery_and_late_resolution_cannot_revive_it() -> None:
    effect_id = await _begin_effect()
    receipt = _receipt("project", "delivery-held-reset")
    await admit_webhook_receipt(
        receipt,
        None,
        effect_evidence=_evidence(revision="2026-07-26T16:00:09+00:00"),
    )
    admission = await admit_conversation_delivery(
        _identity("project", "delivery-held-reset"),
        _subject(),
        GroupFolder("project"),
        payload={"prompt": "held callback"},
    )
    assert admission is not None
    thread_jid = ChatJid("discord:channel:held-reset")
    await store_chat_metadata(thread_jid, receipt.received_at)
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=admission.conversation.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("project"),
            parent_jid=ChatJid("discord:channel:project"),
            thread_jid=thread_jid,
            title="[PYN-1] Held reset",
            updated_at=receipt.received_at,
        )
    )
    cleared_at = datetime.now(UTC).replace(microsecond=999999).isoformat()

    await set_chat_cleared_at(thread_jid, cleared_at)
    await confirm_webhook_effect(effect_id, _evidence())
    stored = await get_conversation_delivery(_identity("project", "delivery-held-reset"))

    assert stored is not None
    assert stored.status is ConversationDeliveryStatus.COMPLETED


async def test_startup_recovery_releases_unsent_effect_but_quarantines_executing_effect() -> None:
    await begin_webhook_effect(_scope())
    prepared_receipt = _receipt("project", "delivery-prepared")
    await admit_webhook_receipt(
        prepared_receipt,
        None,
        effect_evidence=_evidence(revision="2026-07-26T16:00:08+00:00"),
    )
    prepared_delivery = await admit_conversation_delivery(
        _identity("project", "delivery-prepared"),
        _subject(),
        GroupFolder("project"),
    )
    assert prepared_delivery is not None
    await recover_incomplete_webhook_effects()
    recovered_prepared = await get_conversation_delivery(_identity("project", "delivery-prepared"))
    assert recovered_prepared is not None
    assert recovered_prepared.status is ConversationDeliveryStatus.PENDING

    await _begin_effect()
    executing_receipt = _receipt("project", "delivery-executing")
    await admit_webhook_receipt(
        executing_receipt,
        None,
        effect_evidence=_evidence(revision="2026-07-26T16:00:09+00:00"),
    )
    executing_delivery = await admit_conversation_delivery(
        _identity("project", "delivery-executing"),
        _subject(),
        GroupFolder("project"),
    )
    assert executing_delivery is not None
    await recover_incomplete_webhook_effects()
    recovered_executing = await get_conversation_delivery(
        _identity("project", "delivery-executing")
    )

    assert recovered_executing is not None
    assert recovered_executing.status is ConversationDeliveryStatus.HELD


async def test_verified_absent_unknown_effect_releases_held_fifo_head() -> None:
    effect_id = await _begin_effect()
    receipt = _receipt("project", "delivery-unknown")
    await admit_webhook_receipt(
        receipt,
        None,
        effect_evidence=_evidence(revision="2026-07-26T16:00:09+00:00"),
    )
    admission = await admit_conversation_delivery(
        _identity("project", "delivery-unknown"),
        _subject(),
        GroupFolder("project"),
    )
    assert admission is not None
    await mark_webhook_effect_outcome_unknown(effect_id)

    resolution = await reconcile_webhook_effect_absent(effect_id)
    released = await get_conversation_delivery(_identity("project", "delivery-unknown"))

    assert len(resolution.wakeups) == 1
    assert released is not None
    assert released.status is ConversationDeliveryStatus.PENDING


async def test_atomic_conversation_admission_rolls_back_receipt_when_payload_fails() -> None:
    receipt = _receipt("project", "delivery-rollback")
    request = WebhookConversationRequest(
        identity=_identity("project", "delivery-rollback"),
        subject=_subject(),
        workspace=GroupFolder("project"),
        payload={"not_json": object()},
    )

    with pytest.raises(TypeError, match="JSON serializable"):
        await admit_webhook_conversation(receipt, request)

    assert await get_webhook_receipt("linear", "project", "delivery-rollback") is None
    assert await get_conversation_delivery(request.identity) is None


async def test_nonmatching_callback_releases_only_after_last_candidate_resolves() -> None:
    first_effect = await _begin_effect()
    second_effect = await _begin_effect()
    admission = await admit_webhook_receipt(
        _receipt("project", "delivery-human"),
        None,
        effect_evidence=_evidence(revision="2026-07-26T16:00:09+00:00"),
    )
    delivery = await admit_conversation_delivery(
        _identity("project", "delivery-human"),
        _subject(),
        GroupFolder("project"),
        payload={"prompt": "human callback"},
    )
    assert admission.outbound_effect_held is True
    assert delivery is not None

    first_resolution = await confirm_webhook_effect(first_effect, _evidence())
    still_held = await get_conversation_delivery(_identity("project", "delivery-human"))
    assert first_resolution.wakeups == ()
    assert still_held is not None
    assert still_held.status is ConversationDeliveryStatus.HELD

    second_resolution = await confirm_webhook_effect(second_effect, _evidence())
    released = await get_conversation_delivery(_identity("project", "delivery-human"))
    assert len(second_resolution.wakeups) == 1
    assert released is not None
    assert released.status is ConversationDeliveryStatus.PENDING


async def test_comment_client_begins_before_query_and_confirms_exact_response() -> None:
    calls: list[str] = []

    async def begin(  # noqa: RUF029 - async recorder callback contract.
        _scope: WebhookEffectScope,
    ) -> WebhookEffectId:
        calls.append("begin")
        return WebhookEffectId("effect-1")

    async def executing(  # noqa: RUF029 - async recorder callback contract.
        _effect_id: WebhookEffectId,
    ) -> None:
        calls.append("executing")

    async def confirm(  # noqa: RUF029 - async recorder callback contract.
        _effect_id: WebhookEffectId,
        _evidence: object,
    ) -> None:
        calls.append("confirm")

    recorder = LinearSelfEchoRecorder(
        account_name="linear-project",
        begin=begin,
        mark_executing=executing,
        confirm=confirm,
        fail=AsyncMock(),
        mark_outcome_unknown=AsyncMock(),
    )
    client = LinearClient(
        api_key="lin_api_test",  # pragma: allowlist secret
        session=AsyncMock(),
        self_echo_recorder=recorder,
    )

    async def query(  # noqa: RUF029 - async provider client contract.
        _query: str,
        **_variables: object,
    ):
        calls.append("query")
        return {
            "commentCreate": {
                "success": True,
                "comment": {
                    "id": "comment-1",
                    "createdAt": _RECEIVED_AT,
                    "updatedAt": _REVISION,
                    "issue": {"id": _ISSUE_ID},
                },
            }
        }

    client.query = query

    await client.create_comment(_ISSUE_ID, "Validation passed.")

    assert calls == ["begin", "executing", "query", "confirm"]


@pytest.mark.action("linear.comment.create")
async def test_host_comment_action_preserves_workspace_and_provider_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.create_comment = AsyncMock(
        return_value={
            "id": "comment-1",
            "issueId": _ISSUE_ID,
            "createdAt": _RECEIVED_AT,
            "updatedAt": _REVISION,
        }
    )
    workspace_issue = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_comment_actions.linear_client",
        lambda *, workspace: _LinearClientContext(client),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_comment_actions.workspace_issue",
        workspace_issue,
    )

    result = await handle_create_comment(
        {
            "source_group": "project",
            "issue_id": _ISSUE_ID,
            "body": "Validation passed.",
        }
    )

    assert result["result"] == client.create_comment.return_value
    workspace_issue.assert_awaited_once_with(client, "project", _ISSUE_ID)
    client.create_comment.assert_awaited_once_with(_ISSUE_ID, "Validation passed.")


async def test_transport_failure_stays_quarantined_as_outcome_unknown() -> None:
    recorder = LinearSelfEchoRecorder(
        account_name="linear-project",
        begin=AsyncMock(return_value=WebhookEffectId("effect-1")),
        mark_executing=AsyncMock(),
        confirm=AsyncMock(),
        fail=AsyncMock(),
        mark_outcome_unknown=AsyncMock(),
    )
    client = LinearClient(
        api_key="lin_api_test",  # pragma: allowlist secret
        session=AsyncMock(),
        self_echo_recorder=recorder,
    )
    client.query = AsyncMock(side_effect=LinearError("transport outcome is unknown"))

    with pytest.raises(LinearError, match="outcome is unknown"):
        await client.create_comment(_ISSUE_ID, "Validation passed.")

    recorder.mark_outcome_unknown.assert_awaited_once_with(WebhookEffectId("effect-1"))
    recorder.fail.assert_not_awaited()
