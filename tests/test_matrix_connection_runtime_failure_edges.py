"""Failure-boundary tests for routed Matrix connection runtimes."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from pynchy.config.api import MatrixConnectionConfig, MatrixEndpointConfig
from pynchy.conversation.api import ConversationId
from pynchy.host.orchestrator.workspace_config import clear_runtime_workspace_policies
from pynchy.identifiers import ChatJid
from pynchy.plugins.api import ConnectionRuntimeContext
from pynchy.plugins.integrations.matrix_connection import (
    MatrixConnectionOperations,
    MatrixConnectionRuntime,
    MatrixPendingDelivery,
    MatrixRouteControl,
)
from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixGatewayError,
    MatrixPortalAssertion,
    MatrixSyncBatch,
    MatrixSyncEvent,
)
from pynchy.plugins.integrations.matrix_route_registry import clear_active_matrix_routes
from pynchy.plugins.integrations.matrix_route_resolution import ResolvedMatrixRoute


@pytest.fixture(autouse=True)
def _database_and_registries() -> None:
    clear_active_matrix_routes()
    clear_runtime_workspace_policies()
    yield
    clear_active_matrix_routes()
    clear_runtime_workspace_policies()


class _StubGateway:
    def __init__(self, batches: list[MatrixSyncBatch | BaseException]) -> None:
        self.batches = batches

    def sync(self, *, since: str | None, room_ids: tuple[str, ...]) -> MatrixSyncBatch:
        response = self.batches.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def room_assertion(self, *, room_id: str) -> MatrixPortalAssertion:
        return _portal()


def _portal(**changes: object) -> MatrixPortalAssertion:
    values: dict[str, object] = {
        "room_id": "!family:matrix.example.com",
        "owner_user_id": "@me:matrix.example.com",
        "joined": True,
        "bridge": "whatsapp",
        "active_portal": True,
    }
    values.update(changes)
    return MatrixPortalAssertion.model_validate(values)


def _route() -> ResolvedMatrixRoute:
    connection = MatrixConnectionConfig(
        expected_user_id="@me:matrix.example.com",
        chat={
            "family": MatrixEndpointConfig(
                room_id="!family:matrix.example.com",
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
        activation="on_event",
        outbound="approval_required",
        tools=("matrix_route_read",),
        capabilities={},
    )


def _batch(cursor: str, *events: MatrixSyncEvent) -> MatrixSyncBatch:
    return MatrixSyncBatch(next_batch=cursor, events=events, rooms=(_portal(),))


def _event(event_id: str, **changes: object) -> MatrixSyncEvent:
    values: dict[str, object] = {
        "room_id": "!family:matrix.example.com",
        "event_id": event_id,
        "sender": "@friend:matrix.example.com",
        "origin_server_ts": 1,
        "event_type": "m.room.message",
        "message_type": "m.text",
        "body": "hello",
        "decrypted": True,
        "live": True,
    }
    values.update(changes)
    return MatrixSyncEvent.model_validate(values)


def _operations() -> MatrixConnectionOperations:
    return MatrixConnectionOperations(
        get_cursor=AsyncMock(return_value=None),
        set_cursor=AsyncMock(),
        admit_receipt=AsyncMock(),
        admit_delivery=AsyncMock(),
        ensure_route_control=AsyncMock(
            return_value=MatrixRouteControl(
                ConversationId("conversation-1"), ChatJid("discord:channel:matrix-family")
            )
        ),
        list_pending_conversation_ids=AsyncMock(return_value=[]),
        claim_delivery=AsyncMock(return_value=None),
        release_delivery_claim=AsyncMock(),
        conversation_exists=AsyncMock(return_value=True),
        unregister_workspace_restriction=Mock(),
    )


def _context() -> ConnectionRuntimeContext:
    return ConnectionRuntimeContext(
        channels=list,
        workspaces=dict,
        register_workspace=AsyncMock(),
        unregister_workspace=AsyncMock(),
        bind_session=AsyncMock(),
        ingest_message=AsyncMock(),
    )


def _runtime(stub: _StubGateway, operations: MatrixConnectionOperations) -> MatrixConnectionRuntime:
    return MatrixConnectionRuntime(
        "personal-chats",
        (_route(),),
        poll_interval_seconds=999.0,
        state_dir=Path("/state"),
        operations=operations,
        client=stub,
    )


def _conversation_id() -> ConversationId:
    return ConversationId("conversation-1")


async def test_poll_rejects_events_from_unconfigured_rooms() -> None:
    runtime = _runtime(
        _StubGateway(
            [_batch("cursor-1", _event("$foreign", room_id="!foreign:matrix.example.com"))]
        ),
        _operations(),
    )
    with pytest.raises(MatrixGatewayError, match="unconfigured room"):
        await runtime.start(_context())


async def test_poll_ignores_pending_ids_that_are_no_longer_claimable() -> None:
    operations = replace(
        _operations(),
        list_pending_conversation_ids=AsyncMock(return_value=[ConversationId("missing")]),
        claim_delivery=AsyncMock(return_value=None),
    )
    runtime = _runtime(_StubGateway([_batch("cursor-1")]), operations)
    await runtime.start(_context())
    assert runtime.is_ready() is True
    await runtime.close()


async def test_poll_releases_claim_when_sanitized_payload_is_invalid() -> None:
    conversation_id = _conversation_id()
    release = AsyncMock()

    operations = replace(
        _operations(),
        list_pending_conversation_ids=AsyncMock(return_value=[conversation_id]),
        claim_delivery=AsyncMock(
            return_value=MatrixPendingDelivery("$invalid", {"body": None, "sender": "sender"})
        ),
        release_delivery_claim=release,
    )
    runtime = _runtime(_StubGateway([_batch("cursor-1")]), operations)
    with pytest.raises(TypeError, match="sanitized message payload"):
        await runtime.start(_context())
    release.assert_awaited_once()
    await runtime.close()


@pytest.mark.parametrize("failure", [RuntimeError("ingest failed"), asyncio.CancelledError()])
async def test_poll_releases_claim_when_ingestion_does_not_complete(
    failure: BaseException,
) -> None:
    conversation_id = _conversation_id()
    release = AsyncMock()

    async def fail_ingestion(*_args: object) -> None:
        await asyncio.sleep(0)
        raise failure

    operations = replace(
        _operations(),
        list_pending_conversation_ids=AsyncMock(return_value=[conversation_id]),
        claim_delivery=AsyncMock(
            return_value=MatrixPendingDelivery("$delivery", {"body": "hello", "sender": "sender"})
        ),
        release_delivery_claim=release,
    )
    runtime = _runtime(_StubGateway([_batch("cursor-1")]), operations)
    context = replace(_context(), ingest_message=fail_ingestion)

    with pytest.raises(
        type(failure), match="ingest failed" if isinstance(failure, RuntimeError) else None
    ):
        await runtime.start(context)
    release.assert_awaited_once()
    await runtime.close()


async def test_poll_rejects_delivery_for_missing_conversation() -> None:
    conversation_id = ConversationId("missing")

    operations = replace(
        _operations(),
        list_pending_conversation_ids=AsyncMock(return_value=[conversation_id]),
        claim_delivery=AsyncMock(
            return_value=MatrixPendingDelivery("$delivery", {"body": "hello", "sender": "sender"})
        ),
        conversation_exists=AsyncMock(return_value=False),
    )
    runtime = _runtime(_StubGateway([_batch("cursor-1")]), operations)
    with pytest.raises(RuntimeError, match="missing conversation"):
        await runtime.start(_context())
    await runtime.close()


async def test_poll_rejects_delivery_when_route_binding_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = _conversation_id()

    operations = replace(
        _operations(),
        list_pending_conversation_ids=AsyncMock(return_value=[conversation_id]),
        claim_delivery=AsyncMock(
            return_value=MatrixPendingDelivery("$delivery", {"body": "hello", "sender": "sender"})
        ),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.matrix_connection.bind_active_matrix_route",
        lambda _route: None,
    )
    runtime = _runtime(_StubGateway([_batch("cursor-1")]), operations)
    with pytest.raises(RuntimeError, match="active binding"):
        await runtime.start(_context())
    await runtime.close()


async def test_poll_requires_start_before_reconciling_routes() -> None:
    runtime = _runtime(_StubGateway([_batch("cursor-1")]), _operations())

    with pytest.raises(RuntimeError, match="has not started"):
        await runtime.poll_once()


async def test_background_poller_propagates_cancellation_from_provider() -> None:
    class _CancellingGateway(_StubGateway):
        calls = 0

        def sync(self, *, since: str | None, room_ids: tuple[str, ...]) -> MatrixSyncBatch:
            self.calls += 1
            if self.calls == 1:
                return _batch("cursor-1")
            raise asyncio.CancelledError

    gateway = _CancellingGateway([])
    runtime = MatrixConnectionRuntime(
        "personal-chats",
        (_route(),),
        poll_interval_seconds=0.001,
        state_dir=Path("/state"),
        operations=_operations(),
        client=gateway,
    )

    await runtime.start(_context())
    for _ in range(100):
        if gateway.calls >= 2:
            break
        await asyncio.sleep(0.001)
    assert gateway.calls >= 2
    await runtime.close()
