"""Business tests for provider-neutral routed conversation foundations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from conftest import init_test_database, make_settings

from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationDeliveryAdmission,
    ConversationDeliveryStatus,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ensure_conversation_control,
)
from pynchy.host.orchestrator.startup_handler import prepare_interrupted_turn_recovery
from pynchy.state import (
    WebhookReceipt,
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    admit_webhook_receipt,
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    get_conversation,
    get_conversation_control_binding,
    get_webhook_receipt,
    resolve_conversation,
    set_conversation_session,
    set_workspace_profile,
)
from pynchy.types import (
    ChatJid,
    GroupFolder,
    InboundFetchResult,
    OutboundEvent,
    SessionId,
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _subject(key: str) -> ConversationSubject:
    return ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:tenant-acme:issue"),
        key=ConversationSubjectKey(key),
    )


def _delivery(delivery_id: str) -> ExternalDeliveryIdentity:
    return ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId(delivery_id),
    )


def _webhook_receipt(
    identity: ExternalDeliveryIdentity,
    subject_key: str,
) -> WebhookReceipt:
    return WebhookReceipt(
        provider=identity.provider,
        route=identity.route,
        delivery_id=identity.delivery_id,
        workspace="triage",
        event_type="Issue",
        event_action="update",
        subject_id=subject_key,
        payload_sha256=f"sha-{identity.delivery_id}",
        disposition="notified",
        ignored_reason=None,
        task_id=None,
        occurred_at="2026-07-19T12:00:00+00:00",
        received_at="2026-07-19T12:00:01+00:00",
    )


async def _record_receipt(identity: ExternalDeliveryIdentity, subject_key: str) -> None:
    await admit_webhook_receipt(_webhook_receipt(identity, subject_key), None)


async def _admit(
    delivery_id: str,
    subject_key: str,
    workspace: str = "triage",
) -> ConversationDeliveryAdmission:
    identity = _delivery(delivery_id)
    await _record_receipt(identity, subject_key)
    return await admit_conversation_delivery(
        identity,
        _subject(subject_key),
        GroupFolder(workspace),
    )


async def test_subject_identity_survives_workspace_move_and_keeps_session() -> None:
    subject = _subject("issue-immutable-1")
    original = await resolve_conversation(subject, GroupFolder("triage"))
    with_session = await set_conversation_session(original.id, SessionId("session-123"))

    moved = await resolve_conversation(subject, GroupFolder("engineering"))

    assert moved.id == original.id
    assert moved.workspace == GroupFolder("engineering")
    assert moved.session_id == SessionId("session-123")
    assert moved.subject == subject
    assert "issue-immutable-1" not in moved.id
    assert with_session.created_at == moved.created_at


async def test_authenticated_deliveries_dedupe_and_join_by_stable_subject() -> None:
    first = await _admit("delivery-1", "issue-1")
    second = await _admit("delivery-2", "issue-1")

    duplicate = await admit_conversation_delivery(
        _delivery("delivery-1"),
        _subject("issue-1"),
        GroupFolder("another-workspace"),
    )
    separate = await _admit("delivery-3", "issue-2")

    assert first.created is True
    assert second.created is True
    assert duplicate.created is False
    assert first.conversation.id == second.conversation.id == duplicate.conversation.id
    assert first.delivery.sequence < second.delivery.sequence
    assert separate.conversation.id != first.conversation.id

    with pytest.raises(ValueError, match="authenticated receipt"):
        await admit_conversation_delivery(
            _delivery("untrusted-delivery"),
            _subject("issue-1"),
            GroupFolder("triage"),
        )
    with pytest.raises(ValueError, match="another subject"):
        await admit_conversation_delivery(
            _delivery("delivery-1"),
            _subject("issue-2"),
            GroupFolder("triage"),
        )


async def test_webhook_replay_rejects_conflicting_authenticated_bytes() -> None:
    identity = _delivery("delivery-conflict")
    receipt = _webhook_receipt(identity, "issue-1")
    await admit_webhook_receipt(receipt, None)

    with pytest.raises(ValueError, match="conflicting receipt evidence"):
        await admit_webhook_receipt(
            replace(receipt, payload_sha256="sha-different-authenticated-bytes"),
            None,
        )

    retained = await get_webhook_receipt(
        identity.provider,
        identity.route,
        identity.delivery_id,
    )
    assert retained is not None
    assert retained.payload_sha256 == receipt.payload_sha256


async def test_non_webhook_receipt_uses_the_same_provider_neutral_contract() -> None:
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("provider-with-polling"),
        route=ExternalRoute("tenant-a:inbox"),
        delivery_id=ExternalDeliveryId("event-1"),
    )
    receipt = ExternalDeliveryReceipt(
        identity=identity,
        payload_sha256="sha-event-1",
        received_at="2026-07-19T12:00:01+00:00",
    )

    assert await admit_external_delivery_receipt(receipt) is True
    assert await admit_external_delivery_receipt(receipt) is False
    admitted = await admit_conversation_delivery(
        identity,
        ConversationSubject(
            namespace=ConversationSubjectNamespace("provider-with-polling:tenant-a:topic"),
            key=ConversationSubjectKey("topic-9"),
        ),
        GroupFolder("triage"),
    )

    assert admitted.created is True
    assert admitted.delivery.identity == identity
    with pytest.raises(ValueError, match="conflicting receipt"):
        await admit_external_delivery_receipt(
            ExternalDeliveryReceipt(
                identity=identity,
                payload_sha256="different-payload",
                received_at="2026-07-19T12:00:02+00:00",
            )
        )


async def test_claims_serialize_one_conversation_but_not_different_subjects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = await _admit("delivery-a1", "issue-a")
    second = await _admit("delivery-a2", "issue-a")
    other = await _admit("delivery-b1", "issue-b")

    claim_a1 = ConversationClaimId("claim-a1")
    claimed_a1 = await claim_next_conversation_delivery(first.conversation.id, claim_a1)
    blocked_a2 = await claim_next_conversation_delivery(
        first.conversation.id,
        ConversationClaimId("claim-a2-too-early"),
    )
    claimed_b1 = await claim_next_conversation_delivery(
        other.conversation.id,
        ConversationClaimId("claim-b1"),
    )

    assert claimed_a1 is not None
    assert claimed_a1.identity == first.delivery.identity
    assert blocked_a2 is None
    assert claimed_b1 is not None
    assert claimed_b1.identity == other.delivery.identity

    monkeypatch.setattr(
        "pynchy.host.orchestrator.startup_handler.get_settings",
        lambda: make_settings(data_dir=tmp_path),
    )
    await prepare_interrupted_turn_recovery()
    reclaimed_a1 = await claim_next_conversation_delivery(
        first.conversation.id,
        ConversationClaimId("claim-a1-after-restart"),
    )
    assert reclaimed_a1 is not None
    assert reclaimed_a1.identity == first.delivery.identity

    completed = await complete_conversation_delivery(ConversationClaimId("claim-a1-after-restart"))
    claimed_a2 = await claim_next_conversation_delivery(
        first.conversation.id,
        ConversationClaimId("claim-a2"),
    )
    assert completed is not None
    assert completed.status is ConversationDeliveryStatus.COMPLETED
    assert claimed_a2 is not None
    assert claimed_a2.identity == second.delivery.identity


class _DiscordThreadChannel:
    name = "connection.discord.main"
    formatter = object()

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str], str] = {}
        self.created: list[tuple[str, str, str]] = []

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:channel:")

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        return InboundFetchResult(messages=[])

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        return self.threads.get((parent_jid, name))

    async def create_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> str:
        assert participant_ids == ()
        thread_jid = f"discord:channel:thread-{len(self.created) + 1}"
        self.threads[parent_jid, name] = thread_jid
        self.created.append((parent_jid, name, thread_jid))
        return thread_jid


async def _register_workspace(jid: str, folder: str) -> None:
    await set_workspace_profile(
        WorkspaceProfile(
            jid=jid,
            name=folder.title(),
            folder=folder,
            trigger="@Pynchy",
            added_at=datetime.now(UTC).isoformat(),
        )
    )


async def test_deleted_control_thread_is_replaced_and_workspace_can_rebind() -> None:
    triage_jid = ChatJid("discord:channel:triage")
    engineering_jid = ChatJid("discord:channel:engineering")
    await _register_workspace(triage_jid, "triage")
    await _register_workspace(engineering_jid, "engineering")
    conversation = await resolve_conversation(_subject("issue-42"), GroupFolder("triage"))
    await set_conversation_session(conversation.id, SessionId("session-42"))
    channel = _DiscordThreadChannel()
    readable_title = "[ENG-42] Repair scheduler recovery"

    first = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=triage_jid,
            title=readable_title,
        ),
    )
    del channel.threads[triage_jid, readable_title]
    replacement = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=triage_jid,
            title=readable_title,
        ),
    )
    moved = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder("engineering"),
            parent_jid=engineering_jid,
            title=readable_title,
        ),
    )

    current = await get_conversation(conversation.id)
    binding = await get_conversation_control_binding(conversation.id)
    assert first.binding.thread_jid != replacement.binding.thread_jid
    assert replacement.binding.thread_jid != moved.binding.thread_jid
    assert binding == moved.binding
    assert binding.title == readable_title
    assert conversation.id not in binding.title
    assert current is not None
    assert current.id == conversation.id
    assert current.workspace == GroupFolder("engineering")
    assert current.session_id == SessionId("session-42")


async def test_control_titles_use_human_friendly_suffixes_for_distinct_conversations() -> None:
    parent_jid = ChatJid("discord:channel:triage")
    await _register_workspace(parent_jid, "triage")
    first = await resolve_conversation(_subject("family-route"), GroupFolder("triage"))
    second = await resolve_conversation(_subject("renamed-family-route"), GroupFolder("triage"))
    channel = _DiscordThreadChannel()

    first_control = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=first.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=parent_jid,
            title="Family",
        ),
    )
    renamed_route_control = await ensure_conversation_control(
        [channel],
        ConversationControlRequest(
            conversation_id=second.id,
            parent_workspace=GroupFolder("triage"),
            parent_jid=parent_jid,
            title="Family",
        ),
    )

    assert first_control.binding.title == "Family"
    assert renamed_route_control.binding.title == "Family (2)"
    assert first_control.binding.thread_jid != renamed_route_control.binding.thread_jid
    assert [created[1] for created in channel.created] == ["Family", "Family (2)"]
    assert "conv_" not in renamed_route_control.binding.title
