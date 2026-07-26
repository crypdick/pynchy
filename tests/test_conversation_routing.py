"""Business tests for provider-neutral routed conversation foundations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import pytest
from conftest import init_test_database, make_settings

from pynchy.conversation.dispatch import (
    register_conversation_delivery_waker,
    unregister_conversation_delivery_waker,
)
from pynchy.conversation.models import (
    ControlSurface,
    ConversationClaimId,
    ConversationControlBinding,
    ConversationDeliveryAdmission,
    ConversationDeliveryCompletion,
    ConversationDeliveryStatus,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ensure_conversation_control,
)
from pynchy.host.orchestrator.session_handler import send_clear_confirmation
from pynchy.host.orchestrator.startup_handler import prepare_interrupted_turn_recovery
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
from pynchy.state import (
    WebhookReceipt,
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    admit_webhook_receipt,
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_delivery,
    get_session,
    get_session_security_taint,
    get_webhook_receipt,
    get_workspace_profile,
    mark_session_security_taint,
    prepare_conversation_delivery_recovery,
    prepare_conversation_runtime_ownership_recovery,
    resolve_conversation,
    set_chat_cleared_at,
    set_conversation_control_binding,
    set_conversation_session,
    set_session,
    set_workspace_profile,
    store_chat_metadata,
)
from pynchy.types import (
    Channel,
    ChatJid,
    GroupFolder,
    InboundFetchResult,
    OutboundEvent,
    SessionId,
    WorkspaceProfile,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pynchy.event_bus import Event
    from pynchy.host.orchestrator.concurrency import GroupQueue


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
    *,
    received_at: str = "2026-07-19T12:00:01+00:00",
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
        received_at=received_at,
    )


async def _record_receipt(identity: ExternalDeliveryIdentity, subject_key: str) -> None:
    await admit_webhook_receipt(_webhook_receipt(identity, subject_key), None)


async def _admit(
    delivery_id: str,
    subject_key: str,
    workspace: str = "triage",
    *,
    received_at: str = "2026-07-19T12:00:01+00:00",
) -> ConversationDeliveryAdmission:
    identity = _delivery(delivery_id)
    await admit_webhook_receipt(
        _webhook_receipt(identity, subject_key, received_at=received_at),
        None,
    )
    return await admit_conversation_delivery(
        identity,
        _subject(subject_key),
        GroupFolder(workspace),
    )


async def _bind_control_thread(
    conversation_id: ConversationId,
    thread_jid: ChatJid,
) -> None:
    await store_chat_metadata(thread_jid, "2026-07-19T12:00:00+00:00")
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=conversation_id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("triage"),
            parent_jid=ChatJid("discord:channel:triage"),
            thread_jid=thread_jid,
            title="[SYN-9] Reset delivery ordering",
            updated_at="2026-07-19T12:00:00+00:00",
        )
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


async def test_clear_boundary_retires_older_work_and_forgets_routed_session() -> None:
    thread_jid = ChatJid("discord:channel:thread-reset")
    conversation = await resolve_conversation(_subject("issue-reset"), GroupFolder("triage"))
    await _bind_control_thread(conversation.id, thread_jid)
    await set_conversation_session(conversation.id, SessionId("stale-session"))
    claimed = await _admit(
        "delivery-reset-claimed",
        "issue-reset",
        received_at="2098-12-31T23:59:57+00:00",
    )
    pending_before = await _admit(
        "delivery-reset-pending-before",
        "issue-reset",
        received_at="2098-12-31T23:59:58+00:00",
    )
    pending_after = await _admit(
        "delivery-reset-pending-after",
        "issue-reset",
        received_at="2099-01-01T00:00:01+00:00",
    )
    assert await claim_next_conversation_delivery(
        conversation.id,
        ConversationClaimId("claim-orphaned-by-reset"),
    )

    completions = await set_chat_cleared_at(thread_jid, "2099-01-01T00:00:00+00:00")

    routed = await get_conversation(conversation.id)
    retired_claim = await get_conversation_delivery(claimed.delivery.identity)
    retired_pending = await get_conversation_delivery(pending_before.delivery.identity)
    retained_pending = await get_conversation_delivery(pending_after.delivery.identity)
    assert routed is not None
    assert routed.session_id is None
    assert retired_claim is not None
    assert retired_claim.status is ConversationDeliveryStatus.COMPLETED
    assert retired_pending is not None
    assert retired_pending.status is ConversationDeliveryStatus.COMPLETED
    assert retained_pending is not None
    assert retained_pending.status is ConversationDeliveryStatus.PENDING
    assert [completion.conversation_id for completion in completions] == [conversation.id]


async def test_startup_recovery_repairs_legacy_reset_orphan() -> None:
    thread_jid = ChatJid("discord:channel:thread-recovery")
    conversation = await resolve_conversation(
        _subject("issue-reset-recovery"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(conversation.id, thread_jid)
    await set_chat_cleared_at(thread_jid, "2099-01-01T00:00:00+00:00")

    # Reconstruct state left by the pre-fix reset race: a stale routed session,
    # one orphaned claim, and an older pending sibling behind the clear boundary.
    await set_conversation_session(conversation.id, SessionId("legacy-stale-session"))
    claimed = await _admit(
        "delivery-recovery-claimed",
        "issue-reset-recovery",
        received_at="2098-12-31T23:59:57+00:00",
    )
    pending_before = await _admit(
        "delivery-recovery-pending-before",
        "issue-reset-recovery",
        received_at="2098-12-31T23:59:58+00:00",
    )
    pending_after = await _admit(
        "delivery-recovery-pending-after",
        "issue-reset-recovery",
        received_at="2099-01-01T00:00:01+00:00",
    )
    assert await claim_next_conversation_delivery(
        conversation.id,
        ConversationClaimId("legacy-orphaned-claim"),
    )

    recovered = await prepare_conversation_delivery_recovery()

    routed = await get_conversation(conversation.id)
    retired_claim = await get_conversation_delivery(claimed.delivery.identity)
    retired_pending = await get_conversation_delivery(pending_before.delivery.identity)
    retained_pending = await get_conversation_delivery(pending_after.delivery.identity)
    assert recovered == 2
    assert routed is not None
    assert routed.session_id is None
    assert retired_claim is not None
    assert retired_claim.status is ConversationDeliveryStatus.COMPLETED
    assert retired_pending is not None
    assert retired_pending.status is ConversationDeliveryStatus.COMPLETED
    assert retained_pending is not None
    assert retained_pending.status is ConversationDeliveryStatus.PENDING


async def test_startup_recovery_consolidates_legacy_scheduled_session() -> None:
    thread_jid = ChatJid("discord:channel:thread-scheduled")
    conversation = await resolve_conversation(
        _subject("issue-scheduled-session"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(conversation.id, thread_jid)
    copied_session = SessionId("scheduled-session")
    scheduled_folder = dynamic_thread_folder("triage", thread_jid)
    routed_folder = routed_conversation_folder("triage", conversation.id)
    await set_conversation_session(conversation.id, copied_session)
    await set_session(GroupFolder(scheduled_folder), copied_session)
    await mark_session_security_taint(
        GroupFolder(scheduled_folder),
        corruption_tainted=True,
    )
    await set_session(GroupFolder(routed_folder), copied_session)

    legitimate = await resolve_conversation(
        _subject("issue-legitimate-session"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(
        legitimate.id,
        ChatJid("discord:channel:thread-legitimate"),
    )
    legitimate_session = SessionId("legitimate-session")
    legitimate_folder = routed_conversation_folder("triage", legitimate.id)
    await set_conversation_session(legitimate.id, legitimate_session)
    await set_session(GroupFolder(legitimate_folder), legitimate_session)

    recovered = await prepare_conversation_delivery_recovery()

    migrated = await get_conversation(conversation.id)
    preserved = await get_conversation(legitimate.id)
    assert recovered == 1
    assert migrated is not None
    assert migrated.session_id == copied_session
    assert await get_session(GroupFolder(scheduled_folder)) is None
    assert await get_session(GroupFolder(routed_folder)) == copied_session
    legacy_taint = await get_session_security_taint(GroupFolder(scheduled_folder))
    routed_taint = await get_session_security_taint(GroupFolder(routed_folder))
    assert legacy_taint.corruption_tainted is False
    assert routed_taint.corruption_tainted is True
    assert preserved is not None
    assert preserved.session_id == legitimate_session
    assert await get_session(GroupFolder(legitimate_folder)) == legitimate_session


async def test_startup_recovery_migrates_legacy_thread_runtime_ownership() -> None:
    thread_jid = ChatJid("discord:channel:thread-runtime-owner")
    conversation = await resolve_conversation(
        _subject("issue-runtime-owner"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(conversation.id, thread_jid)
    legacy_folder = dynamic_thread_folder("triage", thread_jid)
    routed_folder = routed_conversation_folder("triage", conversation.id)
    await set_workspace_profile(
        WorkspaceProfile(
            jid=thread_jid,
            name="Legacy issue thread",
            folder=legacy_folder,
            trigger="@Pynchy",
        )
    )

    recovered = await prepare_conversation_runtime_ownership_recovery()

    profile = await get_workspace_profile(thread_jid)
    assert recovered == 1
    assert profile is not None
    assert profile.folder == routed_folder


async def test_startup_recovery_does_not_steal_routed_workspace_folder() -> None:
    thread_jid = ChatJid("discord:channel:thread-runtime-conflict")
    conversation = await resolve_conversation(
        _subject("issue-runtime-conflict"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(conversation.id, thread_jid)
    legacy_folder = dynamic_thread_folder("triage", thread_jid)
    routed_folder = routed_conversation_folder("triage", conversation.id)
    legacy = WorkspaceProfile(
        jid=thread_jid,
        name="Legacy issue thread",
        folder=legacy_folder,
        trigger="@Pynchy",
    )
    existing = WorkspaceProfile(
        jid="discord:channel:other-runtime",
        name="Existing routed owner",
        folder=routed_folder,
        trigger="@Pynchy",
    )
    await set_workspace_profile(legacy)
    await set_workspace_profile(existing)
    session_id = SessionId("conflicting-runtime-session")
    await set_conversation_session(conversation.id, session_id)
    await set_session(GroupFolder(legacy_folder), session_id)

    recovered = await prepare_conversation_runtime_ownership_recovery()

    assert recovered == 0
    assert await get_workspace_profile(thread_jid) == legacy
    assert await get_workspace_profile(existing.jid) == existing
    assert await get_session(GroupFolder(legacy_folder)) == session_id
    assert await get_session(GroupFolder(routed_folder)) is None


class _ClearConfirmationDeps:
    def __init__(self) -> None:
        self.sessions: dict[str, str] = {}
        self.session_cleared: set[str] = set()
        self.last_agent_timestamp: dict[str, str] = {}
        self.queue = cast("GroupQueue", object())
        self.channels: list[Channel] = []
        self.workspaces: dict[str, WorkspaceProfile] = {}
        self.events: list[str] = []

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.workspaces[profile.jid] = profile

    async def save_state(self) -> None: ...

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        assert chat_jid == "discord:channel:thread-reset-wake"
        assert text == "🗑️"
        self.events.append("ack")

    def emit(self, _event: Event) -> None:
        self.events.append("cleared")


async def test_reset_ack_precedes_wake_for_delivery_after_clear_boundary() -> None:
    thread_jid = ChatJid("discord:channel:thread-reset-wake")
    conversation = await resolve_conversation(
        _subject("issue-reset-wake"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(conversation.id, thread_jid)
    await _admit(
        "delivery-reset-wake-before",
        "issue-reset-wake",
        received_at="2026-07-19T12:00:01+00:00",
    )
    retained = await _admit(
        "delivery-reset-wake-after",
        "issue-reset-wake",
        received_at="9999-01-01T00:00:00+00:00",
    )
    deps = _ClearConfirmationDeps()
    owner = object()

    async def wake_next(_completion: ConversationDeliveryCompletion) -> None:
        claimed = await claim_next_conversation_delivery(
            conversation.id,
            ConversationClaimId("claim-after-reset-ack"),
        )
        assert claimed is not None
        assert claimed.identity == retained.delivery.identity
        deps.events.append("wake")

    register_conversation_delivery_waker("linear", owner, wake_next)
    try:
        await send_clear_confirmation(deps, thread_jid)
    finally:
        unregister_conversation_delivery_waker("linear", owner)

    assert deps.events == ["cleared", "ack", "wake"]


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
