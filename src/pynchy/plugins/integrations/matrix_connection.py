"""First-party Matrix connection lifecycle and routed event admission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from collections.abc import (
    Awaitable,
    Callable,
    Iterable,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import (
    Path,
)
from typing import TYPE_CHECKING

from pynchy.async_tasks import create_background_task
from pynchy.conversation.api import (
    ConversationClaimId,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
    routed_conversation_folder,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
)
from pynchy.logger import logger
from pynchy.plugins.api import (  # beartype resolves lifecycle annotations.
    ConnectionRuntimeContext,
    NewMessage,
)
from pynchy.plugins.integrations.matrix_event_admission import eligible_matrix_event
from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixConnectionGateway,
    MatrixGatewayError,
    MatrixPortalAssertion,
    MatrixSyncBatch,
    MatrixSyncEvent,
    create_matrix_gateway_client,
)
from pynchy.plugins.integrations.matrix_route_registry import (
    ActiveMatrixRoute,
    bind_active_matrix_route,
    clear_active_matrix_connection,
    get_active_matrix_route,
)
from pynchy.plugins.integrations.matrix_route_resolution import (
    ResolvedMatrixRoute,
)

if TYPE_CHECKING:
    from asyncio import Task

_PROVIDER = ExternalProvider("matrix")


@dataclass(frozen=True, slots=True)
class MatrixPendingDelivery:
    """Sanitized delivery data admitted by the durable conversation store."""

    delivery_id: str
    payload: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class MatrixRouteControl:
    """The routed conversation identity and its durable control thread."""

    conversation_id: ConversationId
    control_thread_jid: ChatJid


@dataclass(frozen=True, slots=True)
class MatrixConnectionOperations:
    """Host-owned state and workspace operations for one Matrix poller."""

    get_cursor: Callable[[str, str], Awaitable[str | None]]
    set_cursor: Callable[[str, str, str], Awaitable[None]]
    admit_receipt: Callable[[ExternalDeliveryReceipt], Awaitable[object]]
    admit_delivery: Callable[
        [ExternalDeliveryIdentity, ConversationSubject, GroupFolder, dict[str, object]],
        Awaitable[None],
    ]
    ensure_route_control: Callable[
        [ConnectionRuntimeContext, ResolvedMatrixRoute, MatrixPortalAssertion],
        Awaitable[MatrixRouteControl],
    ]
    list_pending_conversation_ids: Callable[
        [ExternalProvider, ExternalRoute], Awaitable[Iterable[ConversationId]]
    ]
    claim_delivery: Callable[
        [ConversationId, ConversationClaimId], Awaitable[MatrixPendingDelivery | None]
    ]
    release_delivery_claim: Callable[[ConversationClaimId], Awaitable[object]]
    conversation_exists: Callable[[ConversationId], Awaitable[bool]]
    unregister_workspace_restriction: Callable[[str], None]


def _route_identity(route: ResolvedMatrixRoute) -> ExternalRoute:
    return ExternalRoute(f"{route.connection_name}:{route.name}")


def matrix_route_subject(route: ResolvedMatrixRoute) -> ConversationSubject:
    return ConversationSubject(
        namespace=ConversationSubjectNamespace(
            f"matrix:{route.connection.expected_user_id}:{route.connection_name}:{route.name}:room"
        ),
        key=ConversationSubjectKey(route.endpoint.room_id),
    )


def _validate_portal(
    route: ResolvedMatrixRoute,
    assertion: MatrixPortalAssertion,
) -> None:
    if assertion.room_id != route.endpoint.room_id or not assertion.joined:
        raise MatrixGatewayError(
            f"Matrix route {route.name!r} is not joined at its configured room"
        )
    if assertion.owner_user_id != route.connection.expected_user_id:
        raise MatrixGatewayError(
            f"Matrix route {route.name!r} returned an unexpected owner identity"
        )
    if route.endpoint.expected_bridge is not None and (
        assertion.bridge is None
        or assertion.bridge.casefold() != route.endpoint.expected_bridge.casefold()
    ):
        raise MatrixGatewayError(f"Matrix route {route.name!r} bridge assertion did not match")
    if route.endpoint.require_active_portal and assertion.active_portal is not True:
        raise MatrixGatewayError(f"Matrix route {route.name!r} portal is not active")


class MatrixConnectionRuntime:
    """Poll one owner identity and route its configured rooms fail closed."""

    def __init__(  # noqa: PLR0913 - Matrix runtime binds explicit provider dependencies.
        self,
        connection_name: str,
        routes: tuple[ResolvedMatrixRoute, ...],
        *,
        poll_interval_seconds: float,
        state_dir: Path,
        operations: MatrixConnectionOperations,
        client: MatrixConnectionGateway | None = None,
    ) -> None:
        if not routes:
            raise ValueError("Matrix connection runtime requires at least one enabled route")
        self.name = f"connection.matrix.{connection_name}"
        self._connection_name = connection_name
        self._routes = routes
        self._poll_interval_seconds = poll_interval_seconds
        self._operations = operations
        if client is None:
            command_env = routes[0].connection.gateway_command_env
            command = os.environ.get(command_env)
            client = create_matrix_gateway_client(command, state_dir=state_dir)
        self._client = client
        self._context: ConnectionRuntimeContext | None = None
        self._task: Task[None] | None = None
        self._ready = False

    async def start(self, context: ConnectionRuntimeContext) -> None:
        """Reconcile once before advertising readiness, then poll in background."""
        self._context = context
        await self.poll_once()
        self._ready = True
        self._task = create_background_task(self._run(), name=f"{self.name}-poll")

    async def close(self) -> None:
        """Stop polling without deleting durable receipts or cursor state."""
        self._ready = False
        for folder in clear_active_matrix_connection(self._connection_name):
            self._operations.unregister_workspace_restriction(folder)
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def is_ready(self) -> bool:
        return self._ready

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_seconds)
            try:
                await self.poll_once()
                self._ready = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider corruption must degrade readiness without killing the long-lived poller.
                self._ready = False
                logger.warning(
                    "Matrix connection poll failed",
                    connection=self._connection_name,
                    error_type=type(exc).__name__,
                )

    async def poll_once(self) -> None:
        """Admit one provider page, commit its cursor, and wake queued routes."""
        since = await self._operations.get_cursor("matrix", self._connection_name)
        batch = await asyncio.to_thread(
            self._client.sync,
            since=since,
            room_ids=tuple(route.endpoint.room_id for route in self._routes),
        )
        if since is not None and batch.events and batch.next_batch == since:
            raise MatrixGatewayError("Matrix sync returned events without advancing its cursor")
        assertions = {room.room_id: room for room in batch.rooms}
        for route in self._routes:
            assertion = assertions.get(route.endpoint.room_id)
            if assertion is None:
                raise MatrixGatewayError(f"Matrix route {route.name!r} omitted its room assertion")
            _validate_portal(route, assertion)
            await self._ensure_route_control(route, assertion)
        await self._admit_batch(batch, assertions)
        await self._operations.set_cursor("matrix", self._connection_name, batch.next_batch)
        await self._wake_pending_routes()

    async def _admit_batch(
        self,
        batch: MatrixSyncBatch,
        assertions: dict[str, MatrixPortalAssertion],
    ) -> None:
        routes_by_room = {route.endpoint.room_id: route for route in self._routes}
        for event in batch.events:
            route = routes_by_room.get(event.room_id)
            if route is None:
                raise MatrixGatewayError("Matrix sync returned an unconfigured room")
            if event.live and not event.decrypted:
                raise MatrixGatewayError(
                    f"Matrix route {route.name!r} received an undecryptable live event"
                )
            assertion = assertions[event.room_id]
            if not eligible_matrix_event(event, owner_user_id=assertion.owner_user_id):
                continue
            payload = self._delivery_payload(route, event, assertion)
            identity = ExternalDeliveryIdentity(
                provider=_PROVIDER,
                route=_route_identity(route),
                delivery_id=ExternalDeliveryId(event.event_id),
            )
            received_at = datetime.now(UTC).isoformat()
            await self._operations.admit_receipt(
                ExternalDeliveryReceipt(
                    identity=identity,
                    payload_sha256=hashlib.sha256(
                        json.dumps(payload, sort_keys=True).encode()
                    ).hexdigest(),
                    received_at=received_at,
                )
            )
            if route.activation == "on_demand":
                continue
            # Link the delivery even when the receipt already existed. A crash
            # between those two durable writes must recover on provider replay.
            await self._operations.admit_delivery(
                identity,
                matrix_route_subject(route),
                GroupFolder(route.workspace),
                payload,
            )

    @staticmethod
    def _delivery_payload(
        route: ResolvedMatrixRoute,
        event: MatrixSyncEvent,
        assertion: MatrixPortalAssertion,
    ) -> dict[str, object]:
        return {
            "connection": route.connection_name,
            "route": route.name,
            "endpoint": route.endpoint_name,
            "room_id": event.room_id,
            "event_id": event.event_id,
            "sender": event.sender,
            "origin_server_ts": event.origin_server_ts,
            "body": event.body,
            "portal": assertion.model_dump(mode="json"),
        }

    async def _ensure_route_control(
        self,
        route: ResolvedMatrixRoute,
        assertion: MatrixPortalAssertion,
    ) -> ConversationId:
        control = await self._operations.ensure_route_control(
            self._require_context(), route, assertion
        )
        folder = routed_conversation_folder(route.workspace, control.conversation_id)
        bind_active_matrix_route(
            ActiveMatrixRoute(
                workspace_folder=folder,
                conversation_id=control.conversation_id,
                control_thread_jid=control.control_thread_jid,
                route=route,
                portal=assertion,
            )
        )
        return control.conversation_id

    async def _wake_pending_routes(self) -> None:
        for route in self._routes:
            for conversation_id in await self._operations.list_pending_conversation_ids(
                _PROVIDER, _route_identity(route)
            ):
                await self._wake_one(conversation_id, route)

    async def _wake_one(
        self,
        conversation_id: ConversationId,
        route: ResolvedMatrixRoute,
    ) -> None:
        claim_id = ConversationClaimId(f"matrix_{secrets.token_urlsafe(18)}")
        delivery = await self._operations.claim_delivery(conversation_id, claim_id)
        if delivery is None:
            return
        payload = delivery.payload or {}
        body = payload.get("body")
        sender = payload.get("sender")
        if not isinstance(body, str) or not isinstance(sender, str):
            await self._operations.release_delivery_claim(claim_id)
            raise TypeError("Matrix delivery lost its sanitized message payload")
        active = await self._active_route_for_conversation(conversation_id, route)
        message = NewMessage(
            id=delivery.delivery_id,
            chat_jid=active.control_thread_jid,
            sender=sender,
            sender_name=sender,
            content=body,
            timestamp=datetime.now(UTC).isoformat(),
            is_from_me=False,
            metadata={
                "authenticated_external_route": True,
                "external_provider": "matrix",
                "matrix_route": route.name,
                "conversation_id": conversation_id,
                "conversation_claim_id": claim_id,
            },
        )
        try:
            await self._require_context().ingest_message(active.control_thread_jid, message)
        except asyncio.CancelledError:
            await self._operations.release_delivery_claim(claim_id)
            raise
        except Exception:  # ingestion is the provider delivery boundary.
            await self._operations.release_delivery_claim(claim_id)
            raise

    async def _active_route_for_conversation(
        self,
        conversation_id: ConversationId,
        route: ResolvedMatrixRoute,
    ) -> ActiveMatrixRoute:
        assertion = await asyncio.to_thread(
            self._client.room_assertion,
            room_id=route.endpoint.room_id,
        )
        _validate_portal(route, assertion)
        await self._ensure_route_control(route, assertion)
        if not await self._operations.conversation_exists(conversation_id):
            raise RuntimeError("Matrix delivery references a missing conversation")
        folder = routed_conversation_folder(route.workspace, conversation_id)
        active = get_active_matrix_route(folder)
        if active is None:
            raise RuntimeError("Matrix route reconciliation did not produce an active binding")
        return active

    def _require_context(self) -> ConnectionRuntimeContext:
        if self._context is None:
            raise RuntimeError("Matrix connection runtime has not started")
        return self._context
