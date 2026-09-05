"""Business tests for provider-neutral routed conversation foundations."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from conftest import make_settings

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.conversation.api import dynamic_thread_folder
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
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ensure_conversation_control,
)
from pynchy.host.orchestrator.startup_handler import prepare_interrupted_turn_recovery
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.state import (
    ConversationControlWorkspaceChangedError,
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    admit_webhook_receipt,
    apply_conversation_control_state,
    begin_in_flight_turn,
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_delivery,
    get_in_flight_turn,
    get_session,
    get_session_security_taint,
    get_webhook_receipt,
    mark_session_security_taint,
    prepare_conversation_delivery_recovery,
    resolve_conversation,
    retire_conversation_for_terminal,
    set_chat_cleared_at,
    set_conversation_control_binding,
    set_conversation_session,
    set_session,
)
from tests.conversation_routing_support import (
    _admit,
    _bind_control_thread,
    _delivery,
    _DiscordThreadChannel,
    _register_workspace,
    _subject,
    _webhook_receipt,
)

pytest_plugins = ("tests.conversation_routing_support",)

if TYPE_CHECKING:
    from pathlib import Path


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


async def test_control_binding_does_not_replace_conversation_workspace_owner() -> None:
    admin_jid = ChatJid("discord:channel:admin-control-parent")
    await _register_workspace(admin_jid, "admin")
    conversation = await resolve_conversation(
        _subject("issue-control-parent"),
        GroupFolder("pynchy-dev"),
    )
    ensured = await ensure_conversation_control(
        [_DiscordThreadChannel()],
        ConversationControlRequest(
            conversation_id=conversation.id,
            parent_workspace=GroupFolder("admin"),
            parent_jid=admin_jid,
            title="[SYN-35] Routed control",
            owner_workspace=GroupFolder("pynchy-dev"),
        ),
    )

    stored = await get_conversation(conversation.id)
    binding = await get_conversation_control_binding(conversation.id)
    assert stored is not None
    assert stored.workspace == GroupFolder("pynchy-dev")
    assert binding is not None
    assert binding == ensured.binding
    assert binding.parent_workspace == GroupFolder("admin")


async def test_stale_control_binding_cannot_revert_newer_workspace_owner() -> None:
    conversation = await resolve_conversation(
        _subject("issue-stale-control-owner"),
        GroupFolder("triage"),
    )
    stale_binding = ConversationControlBinding(
        conversation_id=conversation.id,
        surface=ControlSurface.DISCORD,
        parent_workspace=GroupFolder("triage"),
        parent_jid=ChatJid("discord:channel:triage"),
        thread_jid=ChatJid("discord:channel:stale-control-owner"),
        title="[SYN-89] Stale control",
        updated_at="2026-07-27T00:00:00+00:00",
    )

    await resolve_conversation(conversation.subject, GroupFolder("engineering"))

    with pytest.raises(ConversationControlWorkspaceChangedError):
        await set_conversation_control_binding(
            stale_binding,
            owner_workspace=GroupFolder("triage"),
            expected_workspace=GroupFolder("triage"),
        )

    current = await get_conversation(conversation.id)
    assert current is not None
    assert current.workspace == GroupFolder("engineering")
    assert await get_conversation_control_binding(conversation.id) is None


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
    await prepare_interrupted_turn_recovery(
        continuation_path=tmp_path / "deploy_continuation.startup.json"
    )
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


async def test_terminal_retirement_clears_legacy_folders_and_rejects_session_revival() -> None:
    thread_jid = ChatJid("discord:channel:terminal-legacy")
    conversation = await resolve_conversation(
        _subject("issue-terminal-legacy"),
        GroupFolder("owner"),
    )
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("control"),
    )
    await set_conversation_session(conversation.id, SessionId("conversation-session"))
    folders = (
        GroupFolder(routed_conversation_folder(conversation.workspace, conversation.id)),
        GroupFolder(dynamic_thread_folder(conversation.workspace, thread_jid)),
        GroupFolder(dynamic_thread_folder("control", thread_jid)),
    )
    for index, folder in enumerate(folders):
        await set_session(folder, SessionId(f"session-{index}"))
        await mark_session_security_taint(folder, corruption_tainted=True)
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id=f"terminal-turn-{index}",
                chat_jid=thread_jid,
                group_folder=folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-27T00:00:00+00:00",
            )
        )

    retirement = await retire_conversation_for_terminal(
        conversation.id,
        preserve_delivery=_delivery("terminal-lifecycle"),
    )
    retired = await get_conversation(conversation.id)
    late_session = await set_conversation_session(conversation.id, SessionId("late-session"))

    assert set(retirement.runtime_folders) == set(folders)
    assert thread_jid in retirement.runtime_workspace_jids
    assert retired is not None
    assert retired.control_closed is True
    assert retired.session_id is None
    assert late_session.session_id is None
    for index, folder in enumerate(folders):
        assert await get_session(folder) is None
        assert (await get_session_security_taint(folder)).corruption_tainted is False
        assert await get_in_flight_turn(f"terminal-turn-{index}") is None


async def test_unversioned_terminal_cannot_retire_a_versioned_terminal_state() -> None:
    conversation = await resolve_conversation(
        _subject("issue-versioned-terminal"),
        GroupFolder("owner"),
    )
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision="2026-07-27T00:00:02+00:00",
    )

    retirement = await retire_conversation_for_terminal(
        conversation.id,
        preserve_delivery=_delivery("unversioned-terminal"),
    )
    current = await get_conversation(conversation.id)

    assert retirement.is_current is False
    assert retirement.control_state_revision == "2026-07-27T00:00:02+00:00"
    assert current is not None
    assert current.control_closed is True
    assert current.control_state_revision == "2026-07-27T00:00:02+00:00"


async def test_unversioned_open_intent_reopens_an_unversioned_terminal_control() -> None:
    conversation = await resolve_conversation(
        _subject("issue-unversioned-reopen"),
        GroupFolder("owner"),
    )
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision=None,
    )

    applied = await apply_conversation_control_state(
        conversation.id,
        closed=False,
        control_state_revision=None,
    )
    reopened = await get_conversation(conversation.id)

    assert applied is True
    assert reopened is not None
    assert reopened.control_closed is False
    assert reopened.control_state_revision is None


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
