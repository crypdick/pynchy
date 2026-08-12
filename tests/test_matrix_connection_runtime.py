"""Lifecycle and delivery tests for routed Matrix connection runtimes."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import init_test_database, make_settings

from pynchy.config.api import (
    MatrixConnectionConfig,
    MatrixEndpointConfig,
)
from pynchy.conversation.api import routed_conversation_folder
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
from pynchy.host.orchestrator.api import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    RuntimeWorkspaceRestriction,
    ensure_conversation_workspace,
    register_runtime_workspace_restriction,
    unregister_runtime_workspace_restriction,
)
from pynchy.host.orchestrator.startup_handler import prepare_interrupted_turn_recovery
from pynchy.host.orchestrator.workspace_config import clear_runtime_workspace_restrictions
from pynchy.identifiers import GroupFolder
from pynchy.plugins.api import (
    ConnectionRuntimeContext,
    InboundFetchResult,
    NewMessage,
    OutboundEvent,
)
from pynchy.plugins.integrations.matrix_connection import (
    MatrixConnectionOperations,
    MatrixConnectionRuntime,
    MatrixPendingDelivery,
    MatrixRouteControl,
    matrix_route_subject,
)
from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixGatewayError,
    MatrixPortalAssertion,
    MatrixSyncBatch,
    MatrixSyncEvent,
)
from pynchy.plugins.integrations.matrix_route_registry import (
    clear_active_matrix_routes,
    get_active_matrix_route,
)
from pynchy.plugins.integrations.matrix_route_resolution import ResolvedMatrixRoute
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    claim_next_conversation_delivery,
    complete_conversation_delivery,
    delete_workspace_profile,
    get_conversation,
    get_conversation_delivery,
    get_external_delivery_receipt,
    get_external_provider_cursor,
    list_pending_conversation_ids,
    release_conversation_delivery_claim,
    resolve_conversation,
    set_external_provider_cursor,
    set_workspace_profile,
)
from pynchy.workspace.api import CapabilityRule, WorkspaceProfile

_ROOM = "!family:matrix.example.com"
_OWNER = "@me:matrix.example.com"
_SENDER = "@friend:matrix.example.com"
_PARENT_JID = "discord:channel:support"


@pytest.fixture(autouse=True)
async def _database_and_registries() -> None:
    await init_test_database()
    clear_active_matrix_routes()
    clear_runtime_workspace_restrictions()
    yield
    clear_active_matrix_routes()
    clear_runtime_workspace_restrictions()


async def test_provider_cursor_rejects_blank_values_and_replaces_existing_value() -> None:
    assert await get_external_provider_cursor("matrix", "personal-chats") is None
    with pytest.raises(ValueError, match="must not be empty"):
        await set_external_provider_cursor("matrix", "personal-chats", "  ")

    await set_external_provider_cursor("matrix", "personal-chats", "cursor-1")
    await set_external_provider_cursor("matrix", "personal-chats", "cursor-2")

    assert await get_external_provider_cursor("matrix", "personal-chats") == "cursor-2"


class _DiscordThreadChannel:
    name = "connection.discord.main"
    formatter = object()

    def __init__(self) -> None:
        self.threads: dict[tuple[str, str], str] = {}

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
        jid = f"discord:channel:matrix-{len(self.threads) + 1}"
        self.threads[parent_jid, name] = jid
        return jid


@dataclass
class _StubGateway:
    batches: list[MatrixSyncBatch | Exception]
    portal: MatrixPortalAssertion
    sync_calls: list[tuple[str | None, tuple[str, ...]]] = field(default_factory=list)
    status_calls: list[str] = field(default_factory=list)

    def sync(self, *, since: str | None, room_ids: tuple[str, ...]) -> MatrixSyncBatch:
        self.sync_calls.append((since, room_ids))
        if not self.batches:
            raise MatrixGatewayError("no queued sync response")
        response = self.batches.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def room_assertion(self, *, room_id: str) -> MatrixPortalAssertion:
        self.status_calls.append(room_id)
        return self.portal


@dataclass
class _RuntimeHarness:
    channel: _DiscordThreadChannel
    workspaces: dict[str, WorkspaceProfile]
    ingested: list[tuple[str, NewMessage]] = field(default_factory=list)
    sessions: list[tuple[str, str]] = field(default_factory=list)

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.workspaces[profile.jid] = profile
        await set_workspace_profile(profile)

    async def unregister_workspace(self, jid: str) -> None:
        self.workspaces.pop(jid, None)
        await delete_workspace_profile(jid)

    async def bind_session(self, folder: str, session_id: str) -> None:
        self.sessions.append((folder, session_id))

    async def ingest_message(self, jid: str, message: NewMessage) -> None:
        self.ingested.append((jid, message))

    def context(self) -> ConnectionRuntimeContext:
        return ConnectionRuntimeContext(
            channels=lambda: [self.channel],
            workspaces=lambda: self.workspaces,
            register_workspace=self.register_workspace,
            unregister_workspace=self.unregister_workspace,
            bind_session=self.bind_session,
            ingest_message=self.ingest_message,
        )


async def _harness() -> _RuntimeHarness:
    parent = WorkspaceProfile(
        jid=_PARENT_JID,
        name="Support",
        folder="support",
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )
    await set_workspace_profile(parent)
    return _RuntimeHarness(_DiscordThreadChannel(), {_PARENT_JID: parent})


def _operations() -> MatrixConnectionOperations:
    async def admit_delivery(identity: Any, subject: Any, workspace: Any, payload: Any) -> None:
        await admit_conversation_delivery(identity, subject, workspace, payload=payload)

    async def ensure_route_control(
        context: ConnectionRuntimeContext, route: ResolvedMatrixRoute, _assertion: Any
    ) -> MatrixRouteControl:
        parent = next(
            (
                profile
                for profile in context.workspaces().values()
                if profile.folder == route.workspace
            ),
            None,
        )
        if parent is None:
            raise ValueError(f"Matrix route {route.name!r} workspace is not registered")
        conversation = await resolve_conversation(
            matrix_route_subject(route), GroupFolder(route.workspace)
        )
        folder = routed_conversation_folder(route.workspace, conversation.id)
        register_runtime_workspace_restriction(
            folder,
            RuntimeWorkspaceRestriction(
                parent_workspace=route.workspace,
                tools=route.tools,
                capabilities={
                    capability: CapabilityRule(decision=decision)
                    for capability, decision in route.capabilities.items()
                },
            ),
        )
        ensured = await ensure_conversation_workspace(
            ConversationWorkspaceContext(
                channels=context.channels,
                workspaces=context.workspaces,
                register_workspace=context.register_workspace,
                unregister_workspace=context.unregister_workspace,
                bind_session=context.bind_session,
            ),
            ConversationControlRequest(
                conversation_id=conversation.id,
                parent_workspace=GroupFolder(route.workspace),
                parent_jid=parent.jid,
                title=route.control_title,
            ),
        )
        return MatrixRouteControl(conversation.id, ensured.control.binding.thread_jid)

    async def claim_delivery(conversation_id: Any, claim_id: Any) -> MatrixPendingDelivery | None:
        delivery = await claim_next_conversation_delivery(conversation_id, claim_id)
        if delivery is None:
            return None
        return MatrixPendingDelivery(str(delivery.identity.delivery_id), delivery.payload)

    async def conversation_exists(conversation_id: Any) -> bool:
        return await get_conversation(conversation_id) is not None

    return MatrixConnectionOperations(
        get_cursor=get_external_provider_cursor,
        set_cursor=set_external_provider_cursor,
        admit_receipt=admit_external_delivery_receipt,
        admit_delivery=admit_delivery,
        ensure_route_control=ensure_route_control,
        list_pending_conversation_ids=list_pending_conversation_ids,
        claim_delivery=claim_delivery,
        release_delivery_claim=release_conversation_delivery_claim,
        conversation_exists=conversation_exists,
        unregister_workspace_restriction=unregister_runtime_workspace_restriction,
    )


def _portal(**changes: object) -> MatrixPortalAssertion:
    values: dict[str, object] = {
        "room_id": _ROOM,
        "owner_user_id": _OWNER,
        "joined": True,
        "bridge": "whatsapp",
        "active_portal": True,
    }
    values.update(changes)
    return MatrixPortalAssertion.model_validate(values)


def _route(*, activation: str = "on_event") -> ResolvedMatrixRoute:
    connection = MatrixConnectionConfig(
        expected_user_id=_OWNER,
        chat={
            "family": MatrixEndpointConfig(
                room_id=_ROOM,
                title="Family chat",
                expected_bridge="WhatsApp",
                require_active_portal=True,
            )
        },
    )
    return ResolvedMatrixRoute(
        name="family",
        connection_name="personal-chats",
        connection=connection,
        endpoint_name="family",
        endpoint=connection.chat["family"],
        control_title="Family chat",
        workspace="support",
        activation=activation,  # type: ignore[arg-type]
        outbound="approval_required",
        tools=("matrix_route_read", "matrix_route_send"),
        capabilities={},
    )


def _event(event_id: str, **changes: object) -> MatrixSyncEvent:
    values: dict[str, object] = {
        "room_id": _ROOM,
        "event_id": event_id,
        "sender": _SENDER,
        "origin_server_ts": 1,
        "event_type": "m.room.message",
        "message_type": "m.text",
        "body": f"body {event_id}",
        "decrypted": True,
        "live": True,
        "relation_type": None,
        "redacted": False,
    }
    values.update(changes)
    return MatrixSyncEvent.model_validate(values)


def _batch(cursor: str, *events: MatrixSyncEvent, rooms=None) -> MatrixSyncBatch:
    return MatrixSyncBatch(
        next_batch=cursor,
        events=events,
        rooms=(_portal(),) if rooms is None else rooms,
    )


def _identity(event_id: str) -> ExternalDeliveryIdentity:
    return ExternalDeliveryIdentity(
        provider=ExternalProvider("matrix"),
        route=ExternalRoute("personal-chats:family"),
        delivery_id=ExternalDeliveryId(event_id),
    )


def _runtime(
    stub: _StubGateway,
    *,
    activation: str = "on_event",
    interval: float = 999.0,
) -> MatrixConnectionRuntime:
    return MatrixConnectionRuntime(
        "personal-chats",
        (_route(activation=activation),),
        poll_interval_seconds=interval,
        state_dir=Path("/state"),
        operations=_operations(),
        client=stub,
    )


async def test_only_live_original_non_owner_text_is_admitted() -> None:
    eligible = _event("$eligible")
    filtered = (
        _event("$owner", sender=_OWNER),
        _event("$backfill", live=False),
        _event("$edit", relation_type="m.replace"),
        _event("$reaction", event_type="m.reaction", message_type=None, body=None),
        _event("$redacted", redacted=True),
    )
    stub = _StubGateway([_batch("cursor-1", eligible, *filtered)], _portal())
    harness = await _harness()
    runtime = _runtime(stub)

    await runtime.start(harness.context())

    assert [message.id for _, message in harness.ingested] == ["$eligible"]
    assert await get_external_provider_cursor("matrix", "personal-chats") == "cursor-1"
    assert await get_external_delivery_receipt(_identity("$eligible")) is not None
    for event in filtered:
        assert await get_external_delivery_receipt(_identity(event.event_id)) is None
    await runtime.close()


async def test_on_demand_records_receipt_and_cursor_without_delivery_or_wake() -> None:
    stub = _StubGateway([_batch("cursor-1", _event("$one"))], _portal())
    harness = await _harness()
    runtime = _runtime(stub, activation="on_demand")

    await runtime.start(harness.context())

    assert await get_external_delivery_receipt(_identity("$one")) is not None
    assert await get_conversation_delivery(_identity("$one")) is None
    assert await get_external_provider_cursor("matrix", "personal-chats") == "cursor-1"
    assert harness.ingested == []
    await runtime.close()


async def test_on_event_wakes_exactly_one_fifo_delivery_at_a_time() -> None:
    stub = _StubGateway(
        [_batch("cursor-1", _event("$first"), _event("$second")), _batch("cursor-2")],
        _portal(),
    )
    harness = await _harness()
    runtime = _runtime(stub)

    await runtime.start(harness.context())
    assert [message.id for _, message in harness.ingested] == ["$first"]
    first_claim = harness.ingested[0][1].metadata["conversation_claim_id"]
    completed = await complete_conversation_delivery(first_claim)
    assert completed is not None
    assert completed.status is ConversationDeliveryStatus.COMPLETED

    await runtime.poll_once()

    assert [message.id for _, message in harness.ingested] == ["$first", "$second"]
    first = await get_conversation_delivery(_identity("$first"))
    second = await get_conversation_delivery(_identity("$second"))
    assert first is not None
    assert second is not None
    assert first.sequence < second.sequence
    assert second.status is ConversationDeliveryStatus.CLAIMED
    await runtime.close()


async def test_provider_replay_reuses_receipt_delivery_and_conversation() -> None:
    first_stub = _StubGateway([_batch("cursor-1", _event("$same"))], _portal())
    harness = await _harness()
    first_runtime = _runtime(first_stub)
    await first_runtime.start(harness.context())
    first_delivery = await get_conversation_delivery(_identity("$same"))
    assert first_delivery is not None
    await first_runtime.close()

    replay_stub = _StubGateway([_batch("cursor-2", _event("$same"))], _portal())
    replay_runtime = _runtime(replay_stub)
    await replay_runtime.start(harness.context())
    replayed_delivery = await get_conversation_delivery(_identity("$same"))

    assert replay_stub.sync_calls == [("cursor-1", (_ROOM,))]
    assert replayed_delivery == first_delivery
    assert [message.id for _, message in harness.ingested] == ["$same"]
    await replay_runtime.close()


async def test_startup_recovery_releases_and_wakes_a_pending_matrix_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    identity = _identity("$interrupted")
    await admit_external_delivery_receipt(
        ExternalDeliveryReceipt(
            identity=identity,
            payload_sha256="authenticated-payload",
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    admission = await admit_conversation_delivery(
        identity,
        ConversationSubject(
            namespace=ConversationSubjectNamespace(
                "matrix:@me:matrix.example.com:personal-chats:family:room"
            ),
            key=ConversationSubjectKey(_ROOM),
        ),
        GroupFolder("support"),
        payload={"body": "resume me", "sender": _SENDER},
    )
    interrupted_claim = ConversationClaimId("claim-before-restart")
    assert (
        await claim_next_conversation_delivery(admission.conversation.id, interrupted_claim)
        is not None
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.startup_handler.get_settings",
        lambda: make_settings(data_dir=tmp_path),
    )

    await prepare_interrupted_turn_recovery(
        continuation_path=tmp_path / "deploy_continuation.startup.json"
    )
    stub = _StubGateway([_batch("cursor-after-restart")], _portal())
    harness = await _harness()
    runtime = _runtime(stub)
    await runtime.start(harness.context())

    assert [message.id for _, message in harness.ingested] == ["$interrupted"]
    recovered_claim = harness.ingested[0][1].metadata["conversation_claim_id"]
    assert recovered_claim != interrupted_claim
    assert await get_external_provider_cursor("matrix", "personal-chats") == (
        "cursor-after-restart"
    )
    await runtime.close()


async def test_stale_cursor_and_live_decryption_failure_retain_cursor() -> None:
    await set_external_provider_cursor("matrix", "personal-chats", "cursor-old")
    stale = _runtime(_StubGateway([_batch("cursor-old", _event("$stale"))], _portal()))
    harness = await _harness()
    with pytest.raises(MatrixGatewayError, match="without advancing"):
        await stale.start(harness.context())
    assert await get_external_provider_cursor("matrix", "personal-chats") == "cursor-old"
    assert await get_external_delivery_receipt(_identity("$stale")) is None
    await stale.close()

    undecryptable = _runtime(
        _StubGateway(
            [_batch("cursor-new", _event("$encrypted", decrypted=False))],
            _portal(),
        )
    )
    with pytest.raises(MatrixGatewayError, match="undecryptable live event"):
        await undecryptable.start(harness.context())
    assert await get_external_provider_cursor("matrix", "personal-chats") == "cursor-old"
    assert await get_external_delivery_receipt(_identity("$encrypted")) is None
    await undecryptable.close()


@pytest.mark.parametrize(
    ("rooms", "error"),
    [
        ((), "omitted its room assertion"),
        ((_portal(room_id="!other:matrix.example.com"),), "omitted its room assertion"),
        ((_portal(owner_user_id="@other:matrix.example.com"),), "unexpected owner"),
        ((_portal(bridge="signal"),), "bridge assertion"),
        ((_portal(active_portal=False),), "portal is not active"),
    ],
)
async def test_initial_reconciliation_rejects_missing_or_mismatched_portal(
    rooms: tuple[MatrixPortalAssertion, ...],
    error: str,
) -> None:
    runtime = _runtime(_StubGateway([_batch("cursor-1", rooms=rooms)], _portal()))
    harness = await _harness()

    with pytest.raises(MatrixGatewayError, match=error):
        await runtime.start(harness.context())

    assert runtime.is_ready() is False
    assert await get_external_provider_cursor("matrix", "personal-chats") is None
    await runtime.close()


async def test_readiness_tracks_initial_poll_background_failure_and_close() -> None:
    stub = _StubGateway(
        [_batch("cursor-1"), MatrixGatewayError("provider unavailable")],
        _portal(),
    )
    harness = await _harness()
    runtime = _runtime(stub, interval=0.001)

    await runtime.start(harness.context())
    assert runtime.is_ready() is True
    await asyncio.sleep(0.02)
    assert runtime.is_ready() is False

    generated = next(
        profile for profile in harness.workspaces.values() if profile.folder != "support"
    )
    assert get_active_matrix_route(generated.folder) is not None
    await runtime.close()
    assert runtime.is_ready() is False
    assert get_active_matrix_route(generated.folder) is None


async def test_background_poller_recovers_after_unexpected_provider_exception() -> None:
    @dataclass
    class _RecoveringGateway(_StubGateway):
        calls: int = 0
        recovery_started: threading.Event = field(default_factory=threading.Event)
        allow_recovery: threading.Event = field(default_factory=threading.Event)

        def sync(self, *, since: str | None, room_ids: tuple[str, ...]) -> MatrixSyncBatch:
            self.calls += 1
            if self.calls == 1:
                return _batch("cursor-1")
            if self.calls == 2:
                raise TypeError("corrupt provider response")
            self.recovery_started.set()
            self.allow_recovery.wait()
            return _batch(f"cursor-{self.calls}")

    gateway = _RecoveringGateway([], _portal())
    harness = await _harness()
    runtime = _runtime(gateway, interval=0.01)

    try:
        await runtime.start(harness.context())
        assert await asyncio.to_thread(gateway.recovery_started.wait, 1)
        assert runtime.is_ready() is False
        gateway.allow_recovery.set()
        for _ in range(100):
            if runtime.is_ready():
                break
            await asyncio.sleep(0.01)

        assert runtime.is_ready() is True
    finally:
        gateway.allow_recovery.set()
        await runtime.close()


def test_runtime_rejects_connection_without_an_enabled_route() -> None:
    with pytest.raises(ValueError, match="at least one enabled route"):
        MatrixConnectionRuntime(
            "unused",
            (),
            poll_interval_seconds=5.0,
            state_dir=Path("/state"),
            operations=_operations(),
            client=_StubGateway([], _portal()),
        )
