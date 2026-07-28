"""Tests for the durable agent-turn recovery ledger."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.conversation.models import (
    ConversationClaimId,
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
from pynchy.identifiers import GroupFolder
from pynchy.plugins.api import NewMessage
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    begin_in_flight_turn,
    claim_in_flight_turn,
    claim_next_conversation_delivery,
    clear_unclaimed_in_flight_turn_for_task,
    complete_in_flight_turn,
    consume_in_flight_control_message,
    finalize_in_flight_pause,
    get_conversation_delivery,
    get_in_flight_turn,
    get_in_flight_turn_for_chat,
    get_in_flight_turn_for_task,
    get_messages_since,
    get_oldest_resumable_turn_for_group,
    get_router_state,
    init_test_database,
    mark_in_flight_output_sent,
    prepare_conversation_delivery_recovery,
    prepare_in_flight_turn_recovery,
    request_in_flight_turn_control,
    resume_paused_in_flight_turn,
    store_message,
    update_in_flight_session,
)


def _turn(
    turn_id: str = "turn-1",
    *,
    work_kind: InFlightWorkKind = InFlightWorkKind.INTERACTIVE,
    task_id: str | None = None,
    conversation_claim_id: str | None = None,
    input_source: str = "user",
    claimed_at: str | None = "2026-07-14T10:00:02+00:00",
) -> InFlightTurn:
    return InFlightTurn(
        turn_id=turn_id,
        chat_jid="slack:C123",
        group_folder="group-one",
        work_kind=work_kind,
        input_messages=[{"sender_name": "Alice", "content": "finish the job"}],
        input_start_cursor="2026-07-14T09:59:00+00:00",
        input_end_cursor="2026-07-14T10:00:00+00:00",
        started_at="2026-07-14T10:00:01+00:00",
        task_id=task_id,
        session_id="session-before",
        claimed_at=claimed_at,
        conversation_claim_id=conversation_claim_id,
        input_source=input_source,
    )


@pytest.mark.asyncio
async def test_round_trips_turn_and_domain_lookups() -> None:
    await init_test_database()
    interactive = _turn()
    scheduled = _turn(
        "turn-2",
        work_kind=InFlightWorkKind.SCHEDULED,
        task_id="task-2",
    )

    await begin_in_flight_turn(interactive)
    await begin_in_flight_turn(scheduled)

    assert await get_in_flight_turn("turn-1") == interactive
    assert (
        await get_in_flight_turn_for_chat(
            "slack:C123",
            {InFlightWorkKind.INTERACTIVE},
        )
        == interactive
    )
    assert await get_in_flight_turn_for_task("task-2") == scheduled


@pytest.mark.asyncio
async def test_stable_runtime_recovery_selects_oldest_resumable_turn() -> None:
    await init_test_database()
    oldest = replace(
        _turn("turn-oldest"),
        chat_jid="slack:OLD",
        started_at="2026-07-14T10:00:01+00:00",
    )
    newest = replace(
        _turn("turn-newest"),
        chat_jid="slack:CURRENT",
        started_at="2026-07-14T10:00:02+00:00",
    )
    await begin_in_flight_turn(oldest)
    await begin_in_flight_turn(newest)

    recovered = await get_oldest_resumable_turn_for_group(
        "group-one",
        {InFlightWorkKind.INTERACTIVE},
    )

    assert recovered == oldest


@pytest.mark.asyncio
async def test_recovery_releases_old_claim_and_is_claimed_only_once() -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn())

    recovered = await prepare_in_flight_turn_recovery("deploy-sha")

    assert len(recovered) == 1
    assert recovered[0].deploy_id == "deploy-sha"
    assert recovered[0].interrupted_at is not None
    assert recovered[0].claimed_at is None
    assert await claim_in_flight_turn("turn-1") is True
    assert await claim_in_flight_turn("turn-1") is False


@pytest.mark.asyncio
async def test_pause_transition_is_restart_safe_and_resumes_with_guidance() -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn(input_source="external:matrix"))

    requested = await request_in_flight_turn_control(
        "slack:C123",
        CheckpointControlState.PAUSE_REQUESTED,
    )

    assert requested is not None
    assert requested.control_state is CheckpointControlState.PAUSE_REQUESTED
    assert await prepare_in_flight_turn_recovery("deploy-sha") == []
    paused = await get_in_flight_turn("turn-1")
    assert paused is not None
    assert paused.control_state is CheckpointControlState.PAUSED
    assert paused.claimed_at is None
    assert await claim_in_flight_turn("turn-1") is False

    guidance = {
        "message_type": "user",
        "sender": "alice",
        "sender_name": "Alice",
        "content": "Continue, but leave the draft unpublished.",
        "timestamp": "2026-07-14T10:05:00+00:00",
    }
    resumed = await resume_paused_in_flight_turn(
        "turn-1",
        [guidance],
        guidance["timestamp"],
        claim=True,
    )

    assert resumed is not None
    assert resumed.control_state is CheckpointControlState.ACTIVE
    assert resumed.claimed_at is not None
    assert resumed.session_id == "session-before"
    assert resumed.input_source == "external:matrix"
    assert resumed.input_end_cursor == guidance["timestamp"]
    assert resumed.input_messages[-1]["metadata"]["checkpoint_guidance"] is True


@pytest.mark.asyncio
async def test_control_message_consumption_advances_cursor_and_hides_command_atomically() -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn())
    command = NewMessage(
        id="pause-command",
        chat_jid="slack:C123",
        sender="alice",
        sender_name="Alice",
        content="pause",
        timestamp="2026-07-14T10:01:00+00:00",
        metadata={"source": "slack", "deferred_host_control": True},
    )
    await store_message(command)
    cursors = {"slack:C123": "2026-07-14T10:00:00+00:00"}

    turn = await consume_in_flight_control_message(
        command.id,
        command.chat_jid,
        command.timestamp,
        cursors,
        CheckpointControlState.PAUSE_REQUESTED,
    )

    assert turn is not None
    assert turn.control_state is CheckpointControlState.PAUSE_REQUESTED
    stored_messages = await get_messages_since(command.chat_jid, "")
    stored_command = next(message for message in stored_messages if message.id == command.id)
    assert stored_command.message_type == "host"
    assert stored_command.metadata == {"source": "slack"}
    stored_cursor = await get_router_state("last_agent_timestamp")
    assert stored_cursor is not None
    assert json.loads(stored_cursor)[command.chat_jid] == command.timestamp


@pytest.mark.asyncio
async def test_reset_request_never_enters_automatic_recovery() -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn())

    requested = await request_in_flight_turn_control(
        "slack:C123",
        CheckpointControlState.RESET_REQUESTED,
    )

    assert requested is not None
    assert requested.control_state is CheckpointControlState.RESET_REQUESTED
    assert await prepare_in_flight_turn_recovery("deploy-sha") == []
    retained = await get_in_flight_turn("turn-1")
    assert retained is not None
    assert retained.control_state is CheckpointControlState.RESET_REQUESTED


@pytest.mark.asyncio
async def test_terminal_scheduled_cleanup_preserves_claimed_recovery() -> None:
    await init_test_database()
    await begin_in_flight_turn(
        _turn(
            work_kind=InFlightWorkKind.SCHEDULED,
            task_id="task-1",
            claimed_at=None,
        )
    )

    assert await clear_unclaimed_in_flight_turn_for_task("task-1") is True
    assert await get_in_flight_turn("turn-1") is None

    await begin_in_flight_turn(
        _turn(
            "turn-claimed",
            work_kind=InFlightWorkKind.SCHEDULED,
            task_id="task-1",
        )
    )

    assert await clear_unclaimed_in_flight_turn_for_task("task-1") is False
    assert await get_in_flight_turn("turn-claimed") is not None


@pytest.mark.asyncio
async def test_terminal_scheduled_cleanup_preserves_paused_occurrence() -> None:
    await init_test_database()
    await begin_in_flight_turn(
        _turn(
            work_kind=InFlightWorkKind.SCHEDULED,
            task_id="task-1",
            claimed_at=None,
        )
    )
    await request_in_flight_turn_control(
        "slack:C123",
        CheckpointControlState.PAUSE_REQUESTED,
    )
    await finalize_in_flight_pause("turn-1")

    assert await clear_unclaimed_in_flight_turn_for_task("task-1") is False
    paused = await get_in_flight_turn("turn-1")
    assert paused is not None
    assert paused.control_state is CheckpointControlState.PAUSED


@pytest.mark.asyncio
async def test_recovery_preserves_delivery_claim_owned_by_surviving_turn() -> None:
    await init_test_database()
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("matrix"),
        route=ExternalRoute("personal:family"),
        delivery_id=ExternalDeliveryId("$preserved"),
    )
    await admit_external_delivery_receipt(
        ExternalDeliveryReceipt(
            identity=identity,
            payload_sha256="sha",
            received_at="2026-07-19T12:00:00+00:00",
        )
    )
    admission = await admit_conversation_delivery(
        identity,
        ConversationSubject(
            namespace=ConversationSubjectNamespace("matrix:me:family:room"),
            key=ConversationSubjectKey("!family:example.com"),
        ),
        GroupFolder("support"),
    )
    claim_id = ConversationClaimId("claim-survives-restart")
    assert await claim_next_conversation_delivery(admission.conversation.id, claim_id)
    turn = _turn(
        conversation_claim_id=claim_id,
        input_source="external:matrix",
    )
    await begin_in_flight_turn(turn)

    assert await prepare_conversation_delivery_recovery() == 0
    recovered = await prepare_in_flight_turn_recovery("deploy-sha")

    delivery = await get_conversation_delivery(identity)
    assert delivery is not None
    assert delivery.status is ConversationDeliveryStatus.CLAIMED
    assert delivery.claim_id == claim_id
    assert recovered[0].conversation_claim_id == claim_id
    assert recovered[0].input_source == "external:matrix"


@pytest.mark.asyncio
async def test_tracks_session_and_first_user_visible_output() -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn())

    await update_in_flight_session("group-one", "session-after")
    await mark_in_flight_output_sent("turn-1")

    updated = await get_in_flight_turn("turn-1")
    assert updated is not None
    assert updated.session_id == "session-after"
    assert updated.output_sent is True


@pytest.mark.asyncio
async def test_completion_advances_cursor_and_deletes_checkpoint_together() -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn())
    timestamps = {"slack:C123": "2026-07-14T10:00:00+00:00"}

    await complete_in_flight_turn(
        "turn-1",
        last_agent_timestamps=timestamps,
    )

    assert await get_in_flight_turn("turn-1") is None
    stored = await get_router_state("last_agent_timestamp")
    assert stored is not None
    assert json.loads(stored) == timestamps


@pytest.mark.asyncio
async def test_completion_atomically_commits_routed_delivery_and_cursor() -> None:
    await init_test_database()
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("matrix"),
        route=ExternalRoute("personal:family"),
        delivery_id=ExternalDeliveryId("$event"),
    )
    await admit_external_delivery_receipt(
        ExternalDeliveryReceipt(
            identity=identity,
            payload_sha256="sha",
            received_at="2026-07-19T12:00:00+00:00",
        )
    )
    admission = await admit_conversation_delivery(
        identity,
        ConversationSubject(
            namespace=ConversationSubjectNamespace("matrix:me:family:room"),
            key=ConversationSubjectKey("!family:example.com"),
        ),
        GroupFolder("support"),
    )
    claim_id = ConversationClaimId("claim-live")
    assert await claim_next_conversation_delivery(admission.conversation.id, claim_id)
    await begin_in_flight_turn(_turn())
    timestamps = {"slack:C123": "2026-07-14T10:00:00+00:00"}

    await complete_in_flight_turn(
        "turn-1",
        last_agent_timestamps=timestamps,
        conversation_claim_id=claim_id,
    )

    delivery = await get_conversation_delivery(identity)
    assert delivery is not None
    assert delivery.status is ConversationDeliveryStatus.COMPLETED
    assert await get_in_flight_turn("turn-1") is None
    assert json.loads((await get_router_state("last_agent_timestamp")) or "null") == timestamps


@pytest.mark.asyncio
async def test_missing_routed_claim_rolls_back_turn_and_cursor_completion() -> None:
    await init_test_database()
    await begin_in_flight_turn(_turn())

    with pytest.raises(ValueError, match="claim disappeared"):
        await complete_in_flight_turn(
            "turn-1",
            last_agent_timestamps={"slack:C123": "new"},
            conversation_claim_id="missing",
        )

    assert await get_in_flight_turn("turn-1") is not None
    assert await get_router_state("last_agent_timestamp") is None


@pytest.mark.asyncio
async def test_retired_turn_completion_is_a_noop() -> None:
    await init_test_database()

    completed = await complete_in_flight_turn(
        "terminally-retired-turn",
        last_agent_timestamps={"slack:C123": "new"},
        conversation_claim_id="retired-claim",
    )

    assert completed is None
    assert await get_router_state("last_agent_timestamp") is None
