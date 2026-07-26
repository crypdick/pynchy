"""Focused coverage for routed-conversation runtime ownership repair."""

from __future__ import annotations

import pytest
from conftest import init_test_database

from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationDeliveryAdmission,
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
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
from pynchy.state import (
    WebhookReceipt,
    admit_conversation_delivery,
    admit_webhook_receipt,
    begin_in_flight_turn,
    get_conversation,
    get_in_flight_turn,
    get_session,
    get_session_security_taint,
    get_workspace_profile,
    mark_session_security_taint,
    prepare_conversation_runtime_ownership_recovery,
    rebind_conversation_workspace,
    resolve_conversation,
    set_conversation_control_binding,
    set_conversation_session,
    set_session,
    set_workspace_profile,
    store_chat_metadata,
)
from pynchy.state.conversation_runtime_repair import (
    RuntimeOwnershipRepairConflictError,
)
from pynchy.types import (
    ChatJid,
    GroupFolder,
    InFlightTurn,
    InFlightWorkKind,
    SessionId,
    WorkspaceProfile,
)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _subject(key: str) -> ConversationSubject:
    return ConversationSubject(
        namespace=ConversationSubjectNamespace("linear"),
        key=ConversationSubjectKey(key),
    )


async def _admit(
    delivery_id: str,
    subject_key: str,
    *,
    workspace: str,
) -> ConversationDeliveryAdmission:
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("issues"),
        delivery_id=ExternalDeliveryId(delivery_id),
    )
    await admit_webhook_receipt(
        WebhookReceipt(
            provider=identity.provider,
            route=identity.route,
            delivery_id=identity.delivery_id,
            workspace=workspace,
            event_type="Issue",
            event_action="update",
            subject_id=subject_key,
            payload_sha256=f"sha-{delivery_id}",
            disposition="routed",
            ignored_reason=None,
            task_id=None,
            occurred_at="2026-07-19T12:00:00+00:00",
            received_at="2026-07-19T12:00:01+00:00",
        ),
        None,
    )
    admission = await admit_conversation_delivery(
        identity,
        _subject(subject_key),
        GroupFolder(workspace),
    )
    if admission is None:
        raise AssertionError("Test delivery was unexpectedly suppressed")
    return admission


async def _bind_control_thread(
    conversation_id: ConversationId,
    thread_jid: ChatJid,
    *,
    parent_workspace: GroupFolder,
) -> None:
    await store_chat_metadata(thread_jid, "2026-07-19T12:00:00+00:00")
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=conversation_id,
            surface=ControlSurface.DISCORD,
            parent_workspace=parent_workspace,
            parent_jid=ChatJid(f"discord:channel:{parent_workspace}"),
            thread_jid=thread_jid,
            title="[SYN-35] Routed control",
            updated_at="2026-07-19T12:00:00+00:00",
        )
    )


def _turn(
    turn_id: str,
    thread_jid: ChatJid,
    folder: GroupFolder,
    session_id: SessionId,
) -> InFlightTurn:
    return InFlightTurn(
        turn_id=turn_id,
        chat_jid=thread_jid,
        group_folder=folder,
        work_kind=InFlightWorkKind.INTERACTIVE,
        input_messages=[{"content": "resume after deploy"}],
        input_start_cursor="2026-07-19T11:59:00+00:00",
        input_end_cursor="2026-07-19T12:00:00+00:00",
        started_at="2026-07-19T12:00:01+00:00",
        session_id=session_id,
    )


async def test_authenticated_delivery_repairs_corrupt_profile_and_runtime() -> None:
    thread_jid = ChatJid("discord:channel:corrupt-runtime-owner")
    conversation = (
        await _admit(
            "delivery-runtime-owner",
            "issue-corrupt-runtime-owner",
            workspace="pynchy-dev",
        )
    ).conversation
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("admin"),
    )
    corrupt_folder = GroupFolder(routed_conversation_folder("admin", conversation.id))
    await set_workspace_profile(
        WorkspaceProfile(
            jid=thread_jid,
            name="Admin/SYN-35",
            folder=corrupt_folder,
            trigger="@Pynchy",
        )
    )
    await rebind_conversation_workspace(conversation.id, GroupFolder("admin"))
    session_id = SessionId("codex:model:corrupt-owner-thread")
    await set_conversation_session(conversation.id, session_id)
    await set_session(corrupt_folder, session_id)
    turn = _turn("turn-corrupt-runtime-owner", thread_jid, corrupt_folder, session_id)
    await begin_in_flight_turn(turn)

    recovered = await prepare_conversation_runtime_ownership_recovery()

    repaired = await get_conversation(conversation.id)
    profile = await get_workspace_profile(thread_jid)
    repaired_turn = await get_in_flight_turn(turn.turn_id)
    repaired_folder = GroupFolder(routed_conversation_folder("pynchy-dev", conversation.id))
    assert recovered == 4
    assert repaired is not None
    assert repaired.workspace == GroupFolder("pynchy-dev")
    assert profile is not None
    assert profile.folder == repaired_folder
    assert await get_session(corrupt_folder) is None
    assert await get_session(repaired_folder) == session_id
    assert repaired_turn is not None
    assert repaired_turn.group_folder == repaired_folder


async def test_authenticated_delivery_repairs_runtime_without_profile() -> None:
    thread_jid = ChatJid("discord:channel:missing-runtime-profile")
    conversation = (
        await _admit(
            "delivery-missing-runtime-profile",
            "issue-missing-runtime-profile",
            workspace="pynchy-dev",
        )
    ).conversation
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("admin"),
    )
    await rebind_conversation_workspace(conversation.id, GroupFolder("admin"))
    session_id = SessionId("codex:model:missing-profile-thread")
    corrupt_folder = GroupFolder(routed_conversation_folder("admin", conversation.id))
    await set_conversation_session(conversation.id, session_id)
    await set_session(corrupt_folder, session_id)
    turn = _turn("turn-missing-runtime-profile", thread_jid, corrupt_folder, session_id)
    await begin_in_flight_turn(turn)

    recovered = await prepare_conversation_runtime_ownership_recovery()

    repaired = await get_conversation(conversation.id)
    repaired_turn = await get_in_flight_turn(turn.turn_id)
    repaired_folder = GroupFolder(routed_conversation_folder("pynchy-dev", conversation.id))
    assert recovered == 3
    assert repaired is not None
    assert repaired.workspace == GroupFolder("pynchy-dev")
    assert await get_workspace_profile(thread_jid) is None
    assert await get_session(corrupt_folder) is None
    assert await get_session(repaired_folder) == session_id
    assert repaired_turn is not None
    assert repaired_turn.group_folder == repaired_folder


async def test_authoritative_owner_conflict_aborts_without_partial_mutation() -> None:
    thread_jid = ChatJid("discord:channel:conflicting-owner-repair")
    conversation = (
        await _admit(
            "delivery-conflicting-owner-repair",
            "issue-conflicting-owner-repair",
            workspace="pynchy-dev",
        )
    ).conversation
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("admin"),
    )
    original_folder = GroupFolder(routed_conversation_folder("admin", conversation.id))
    target_folder = GroupFolder(routed_conversation_folder("pynchy-dev", conversation.id))
    original_profile = WorkspaceProfile(
        jid=thread_jid,
        name="Corrupt owner",
        folder=original_folder,
        trigger="@Pynchy",
    )
    occupied_profile = WorkspaceProfile(
        jid="discord:channel:existing-target-owner",
        name="Existing target owner",
        folder=target_folder,
        trigger="@Pynchy",
    )
    await set_workspace_profile(original_profile)
    await set_workspace_profile(occupied_profile)
    await rebind_conversation_workspace(conversation.id, GroupFolder("admin"))
    session_id = SessionId("codex:model:conflicting-owner-thread")
    await set_conversation_session(conversation.id, session_id)
    await set_session(original_folder, session_id)
    turn = _turn("turn-conflicting-owner-repair", thread_jid, original_folder, session_id)
    await begin_in_flight_turn(turn)

    with pytest.raises(RuntimeOwnershipRepairConflictError):
        await prepare_conversation_runtime_ownership_recovery()

    unchanged = await get_conversation(conversation.id)
    assert unchanged is not None
    assert unchanged.workspace == GroupFolder("admin")
    assert await get_workspace_profile(thread_jid) == original_profile
    assert await get_workspace_profile(occupied_profile.jid) == occupied_profile
    assert await get_session(original_folder) == session_id
    assert await get_session(target_folder) is None
    assert await get_in_flight_turn(turn.turn_id) == turn


async def test_repair_does_not_overwrite_conflicting_target_session() -> None:
    thread_jid = ChatJid("discord:channel:thread-session-conflict")
    conversation = await resolve_conversation(
        _subject("issue-session-conflict"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("triage"),
    )
    legacy_folder = GroupFolder(dynamic_thread_folder("triage", thread_jid))
    routed_folder = GroupFolder(routed_conversation_folder("triage", conversation.id))
    profile = WorkspaceProfile(
        jid=thread_jid,
        name="Legacy issue thread",
        folder=legacy_folder,
        trigger="@Pynchy",
    )
    await set_workspace_profile(profile)
    session_id = SessionId("conversation-session")
    conflicting_session_id = SessionId("other-runtime-session")
    await set_conversation_session(conversation.id, session_id)
    await set_session(legacy_folder, session_id)
    await set_session(routed_folder, conflicting_session_id)

    assert await prepare_conversation_runtime_ownership_recovery() == 0
    assert await get_workspace_profile(thread_jid) == profile
    assert await get_session(legacy_folder) == session_id
    assert await get_session(routed_folder) == conflicting_session_id


async def test_authenticated_owner_repair_replaces_unreferenced_target_session() -> None:
    thread_jid = ChatJid("discord:channel:stale-target-session")
    conversation = (
        await _admit(
            "delivery-stale-target-session",
            "issue-stale-target-session",
            workspace="pynchy-dev",
        )
    ).conversation
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("admin"),
    )
    source_folder = GroupFolder(routed_conversation_folder("admin", conversation.id))
    target_folder = GroupFolder(routed_conversation_folder("pynchy-dev", conversation.id))
    await set_workspace_profile(
        WorkspaceProfile(
            jid=thread_jid,
            name="Corrupt owner",
            folder=source_folder,
            trigger="@Pynchy",
        )
    )
    await rebind_conversation_workspace(conversation.id, GroupFolder("admin"))
    current_session = SessionId("codex:model:current-conversation-session")
    stale_target_session = SessionId("codex:model:stale-target-session")
    await set_conversation_session(conversation.id, current_session)
    await set_session(source_folder, current_session)
    await set_session(target_folder, stale_target_session)
    await mark_session_security_taint(source_folder, corruption_tainted=True)
    await mark_session_security_taint(target_folder, secret_tainted=True)

    assert await prepare_conversation_runtime_ownership_recovery() == 3

    repaired = await get_conversation(conversation.id)
    assert repaired is not None
    assert repaired.workspace == GroupFolder("pynchy-dev")
    assert await get_session(source_folder) is None
    assert await get_session(target_folder) == current_session
    taint = await get_session_security_taint(target_folder)
    assert taint.corruption_tainted is True
    assert taint.secret_tainted is True


async def test_authenticated_owner_repair_rejects_referenced_target_session() -> None:
    thread_jid = ChatJid("discord:channel:referenced-target-session")
    conversation = (
        await _admit(
            "delivery-referenced-target-session",
            "issue-referenced-target-session",
            workspace="pynchy-dev",
        )
    ).conversation
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("admin"),
    )
    source_folder = GroupFolder(routed_conversation_folder("admin", conversation.id))
    target_folder = GroupFolder(routed_conversation_folder("pynchy-dev", conversation.id))
    await rebind_conversation_workspace(conversation.id, GroupFolder("admin"))
    current_session = SessionId("codex:model:current-session")
    referenced_target_session = SessionId("codex:model:foreign-session")
    await set_conversation_session(conversation.id, current_session)
    await set_session(source_folder, current_session)
    await set_session(target_folder, referenced_target_session)
    foreign = await resolve_conversation(
        _subject("issue-foreign-session-owner"),
        GroupFolder("other-workspace"),
    )
    await set_conversation_session(foreign.id, referenced_target_session)

    with pytest.raises(RuntimeOwnershipRepairConflictError):
        await prepare_conversation_runtime_ownership_recovery()

    unchanged = await get_conversation(conversation.id)
    assert unchanged is not None
    assert unchanged.workspace == GroupFolder("admin")
    assert await get_session(source_folder) == current_session
    assert await get_session(target_folder) == referenced_target_session


async def test_repair_does_not_take_a_source_folder_owned_by_another_jid() -> None:
    thread_jid = ChatJid("discord:channel:thread-source-conflict")
    conversation = await resolve_conversation(
        _subject("issue-source-conflict"),
        GroupFolder("triage"),
    )
    await _bind_control_thread(
        conversation.id,
        thread_jid,
        parent_workspace=GroupFolder("triage"),
    )
    legacy_folder = GroupFolder(dynamic_thread_folder("triage", thread_jid))
    foreign_profile = WorkspaceProfile(
        jid="discord:channel:foreign-source-owner",
        name="Foreign source owner",
        folder=legacy_folder,
        trigger="@Pynchy",
    )
    await set_workspace_profile(foreign_profile)
    session_id = SessionId("conversation-session")
    await set_conversation_session(conversation.id, session_id)
    await set_session(legacy_folder, session_id)
    await mark_session_security_taint(legacy_folder, secret_tainted=True)

    assert await prepare_conversation_runtime_ownership_recovery() == 0
    assert await get_workspace_profile(foreign_profile.jid) == foreign_profile
    assert await get_session(legacy_folder) == session_id
    assert (await get_session_security_taint(legacy_folder)).secret_tainted is True
