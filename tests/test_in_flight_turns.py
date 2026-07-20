"""Tests for the durable agent-turn recovery ledger."""

from __future__ import annotations

import json

import pytest

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
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    begin_in_flight_turn,
    claim_in_flight_turn,
    claim_next_conversation_delivery,
    complete_in_flight_turn,
    get_conversation_delivery,
    get_in_flight_turn,
    get_in_flight_turn_for_chat,
    get_in_flight_turn_for_task,
    get_router_state,
    init_test_database,
    mark_in_flight_output_sent,
    prepare_conversation_delivery_recovery,
    prepare_in_flight_turn_recovery,
    update_in_flight_session,
)
from pynchy.types import GroupFolder, InFlightTurn, InFlightWorkKind


def _turn(
    turn_id: str = "turn-1",
    *,
    work_kind: InFlightWorkKind = InFlightWorkKind.INTERACTIVE,
    task_id: str | None = None,
    scheduled_base_chat_jid: str | None = None,
    scheduled_thread_slot: int | None = None,
    conversation_claim_id: str | None = None,
    input_source: str = "user",
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
        claimed_at="2026-07-14T10:00:02+00:00",
        scheduled_base_chat_jid=scheduled_base_chat_jid,
        scheduled_thread_slot=scheduled_thread_slot,
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
        scheduled_base_chat_jid="slack:C123",
        scheduled_thread_slot=1,
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
