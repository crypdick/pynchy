"""Authenticated plugin webhook ingress and durable task admission."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast, runtime_checkable

from aiohttp import web

from pynchy.conversation.api import (  # noqa: TC001 - beartype resolves dispatch inputs at runtime.
    ConversationId,
)
from pynchy.host.orchestrator.http_control import ClientAddress, RequestRateLimiter
from pynchy.host.orchestrator.webhook_conversations import (
    ConversationWebhookDeps,
    WebhookConversationDispatcher,
)
from pynchy.host.orchestrator.webhook_delivery_admission import (
    WebhookDeliveryAdmissionRequest,
    admit_prepared_event,
    effect_callback_decision,
)
from pynchy.host.orchestrator.webhook_event_rendering import prompt_for_event
from pynchy.host.orchestrator.workspace_placement import resolve_workspace_placement
from pynchy.logger import logger
from pynchy.plugins.api import (
    WebhookAuthenticationError,
    WebhookConfigurationError,
    WebhookDiscard,
    WebhookEvent,
    WebhookPayloadError,
    WebhookProcessingError,
    WebhookRoute,
    validate_webhook_routes,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state.api import (
    WebhookAdmission,
    WebhookReceipt,
    get_webhook_receipt,
)
from pynchy.webhook_effects import WebhookEffectCallbackDecision
from pynchy.workspace.api import (
    WorkspaceProfile,  # noqa: TC001 - beartype resolves contract annotations at runtime.
)


@runtime_checkable
class WebhookIngressDeps(Protocol):
    """Host capabilities required by provider webhook ingress."""

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None: ...

    def get_workspace(self, folder: str) -> WorkspaceProfile | None: ...

    def dispatch_scheduled_task(self, task: ScheduledTask) -> None: ...


@dataclass(frozen=True)
class WebhookIngress:
    """Validated route registry installed into one aiohttp application."""

    deps: WebhookIngressDeps
    routes: dict[str, WebhookRoute]
    limiters: dict[str, RequestRateLimiter]
    conversation_dispatcher: WebhookConversationDispatcher | None

    @property
    def public_paths(self) -> frozenset[str]:
        return frozenset(self.routes)


webhook_ingress_key: web.AppKey[WebhookIngress] = web.AppKey("webhook_ingress")


def _resolve_route_workspace(
    deps: WebhookIngressDeps,
    workspace_name: str,
) -> WorkspaceProfile | None:
    workspace = deps.get_workspace(workspace_name)
    if workspace is not None:
        return workspace
    if not isinstance(deps, ConversationWebhookDeps):
        return None
    placement = resolve_workspace_placement(deps.workspaces().values(), workspace_name)
    return placement.owner if placement is not None else None


def build_webhook_ingress(
    deps: WebhookIngressDeps,
    routes: tuple[WebhookRoute, ...],
) -> WebhookIngress:
    """Validate and assemble one host-owned ingress registry."""
    # NOTE: Update docs/usage/control-plane.md "Provider-authenticated webhooks"
    # and docs/architecture/security.md "HTTP Control Plane" with this boundary.
    for route in routes:
        workspace_names = (
            *((route.workspace,) if route.workspace is not None else ()),
            *route.candidate_workspaces,
        )
        for workspace_name in dict.fromkeys(workspace_names):
            workspace = _resolve_route_workspace(deps, workspace_name)
            if workspace is None:
                raise WebhookConfigurationError(
                    f"Webhook route {route.path} names unknown workspace {workspace_name!r}"
                )
            if workspace.is_admin and not route.allow_admin_workspaces:
                raise WebhookConfigurationError(
                    f"Webhook route {route.path} cannot target admin workspace {workspace_name!r}"
                )
            if route.validate_workspace is not None:
                reason = route.validate_workspace(workspace)
                if reason is not None:
                    raise WebhookConfigurationError(f"Webhook route {route.path} {reason}")
        if route.routes_conversations and not isinstance(deps, ConversationWebhookDeps):
            raise WebhookConfigurationError(
                f"Webhook route {route.path} requires conversation runtime capabilities"
            )
    validated = validate_webhook_routes(routes)
    conversation_routes = tuple(route for route in validated if route.routes_conversations)
    dispatcher = (
        WebhookConversationDispatcher(deps=deps, routes=conversation_routes)
        if conversation_routes and isinstance(deps, ConversationWebhookDeps)
        else None
    )
    return WebhookIngress(
        deps=deps,
        routes={route.path: route for route in validated},
        limiters={
            route.path: RequestRateLimiter(
                request_limit=route.rate_limit_requests,
                window_seconds=route.rate_limit_window_seconds,
            )
            for route in validated
        },
        conversation_dispatcher=dispatcher,
    )


async def _read_bounded_body(request: web.Request, limit: int) -> bytes | None:
    if request.content_length is not None and request.content_length > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.content.iter_chunked(min(64 * 1024, limit + 1)):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _rate_limit_response(
    request: web.Request,
    ingress: WebhookIngress,
    route: WebhookRoute,
) -> web.Response | None:
    rate = ingress.limiters[route.path].consume(ClientAddress(request.remote or "unknown"))
    if rate.allowed:
        return None
    return web.json_response(
        {"error": "rate limit exceeded"},
        status=429,
        headers={"Retry-After": str(rate.retry_after_seconds)},
    )


async def _request_body(
    request: web.Request,
    route: WebhookRoute,
) -> tuple[bytes, str] | web.Response:
    if request.content_type != "application/json":
        return web.json_response({"error": "application/json required"}, status=415)
    raw_body = await _read_bounded_body(request, route.max_body_bytes)
    if raw_body is None:
        return web.json_response({"error": "payload too large"}, status=413)
    secret = os.environ.get(route.secret_env)
    if secret:
        return raw_body, secret
    logger.error("Webhook route secret disappeared after startup", route=route.path)
    return web.json_response({"error": "webhook unavailable"}, status=503)


async def _prepare_route_event(
    route: WebhookRoute,
    event: WebhookEvent,
) -> WebhookEvent | web.Response:
    """Apply one route-owned, read-only admission check on every delivery attempt."""
    if route.prepare_event is None:
        return event
    try:
        return await route.prepare_event(event)
    except WebhookProcessingError as exc:
        logger.warning(
            "Authenticated webhook event preparation failed",
            route=route.path,
            reason=str(exc),
        )
        return web.json_response({"error": "webhook processing failed"}, status=503)


async def _process_route_event(
    route: WebhookRoute,
    event: WebhookEvent,
) -> WebhookEvent | web.Response:
    """Apply one route-owned trusted host effect once before receipt admission."""
    existing = await get_webhook_receipt(route.provider, route.name, event.delivery_id)
    if existing is not None:
        return event
    if route.process_event is None:
        return event
    try:
        return await route.process_event(event)
    except WebhookProcessingError as exc:
        logger.warning(
            "Authenticated webhook host effect failed",
            route=route.path,
            reason=str(exc),
        )
        return web.json_response({"error": "webhook processing failed"}, status=503)


def _parse_event(
    request: web.Request,
    route: WebhookRoute,
    raw_body: bytes,
    secret: str,
    received_at: datetime,
) -> WebhookEvent | WebhookDiscard | web.Response:
    try:
        return route.parse(raw_body, dict(request.headers), secret, received_at)
    except WebhookAuthenticationError as exc:
        logger.warning("Webhook authentication failed", route=route.path, reason=str(exc))
        return web.json_response({"error": "authentication failed"}, status=401)
    except WebhookPayloadError as exc:
        logger.warning("Authenticated webhook payload rejected", route=route.path, reason=str(exc))
        return web.json_response({"error": "invalid payload"}, status=400)


async def _prepared_event_and_workspace(  # noqa: PLR0911 - each admission failure returns its HTTP response.
    request: web.Request,
    ingress: WebhookIngress,
    route: WebhookRoute,
    body: tuple[bytes, str],
    received_at: datetime,
) -> tuple[WebhookEvent, WorkspaceProfile | None] | web.Response:
    raw_body, secret = body
    event = _parse_event(request, route, raw_body, secret, received_at)
    if isinstance(event, web.Response):
        return event
    if isinstance(event, WebhookDiscard):
        return web.Response(status=204)
    prepared_event = await _prepare_route_event(route, event)
    if isinstance(prepared_event, web.Response):
        return prepared_event
    target_workspace = (
        prepared_event.conversation.workspace
        if prepared_event.conversation is not None
        and prepared_event.conversation.workspace is not None
        else route.workspace
    )
    workspace = (
        _resolve_route_workspace(ingress.deps, target_workspace) if target_workspace else None
    )
    if workspace is None and (
        target_workspace is not None or prepared_event.instructions is not None
    ):
        logger.error("Webhook route lost its resolved workspace", route=route.path)
        return web.json_response({"error": "webhook route unavailable"}, status=503)
    if workspace is not None and workspace.is_admin and not route.allow_admin_workspaces:
        logger.error("Webhook route resolved a forbidden admin workspace", route=route.path)
        return web.json_response({"error": "webhook route unavailable"}, status=503)
    if (
        prepared_event.conversation is not None
        and prepared_event.conversation.workspace is not None
        and prepared_event.conversation.workspace not in route.candidate_workspaces
        and prepared_event.conversation.workspace != route.workspace
    ):
        logger.error("Webhook route resolved an undeclared workspace", route=route.path)
        return web.json_response({"error": "webhook route unavailable"}, status=503)
    return prepared_event, workspace


def _task_for_event(
    route: WebhookRoute,
    event: WebhookEvent,
    workspace: WorkspaceProfile | None,
    received_at: str,
) -> ScheduledTask | None:
    if event.instructions is None or event.conversation is not None:
        return None
    workspace = cast("WorkspaceProfile", workspace)
    return ScheduledTask(
        id=f"webhook-{route.provider}-{route.name}-{event.delivery_id}",
        group_folder=workspace.folder,
        chat_jid=workspace.jid,
        prompt=prompt_for_event(route, event),
        schedule_type="once",
        schedule_value=received_at,
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        next_run=received_at,
        created_at=received_at,
        input_source=(
            f"webhook:{route.provider}" if route.public_source else f"trusted:{route.provider}"
        ),
        derived_thread_name=f"Webhook | {route.provider}/{route.name} | {event.delivery_id}"[:100],
    )


async def _prepare_conversation_routes(  # noqa: RUF029 - aiohttp startup hooks use an async callback contract.
    app: web.Application,
) -> None:
    ingress = app[webhook_ingress_key]
    dispatcher = cast("WebhookConversationDispatcher", ingress.conversation_dispatcher)
    dispatcher.prepare()


async def recover_webhook_conversations(app: web.Application) -> None:
    """Restore durable webhook routes and dispatch pending deliveries."""
    ingress = app[webhook_ingress_key]
    if ingress.conversation_dispatcher is not None:
        await ingress.conversation_dispatcher.recover_pending()


async def _stop_conversation_routes(  # noqa: RUF029 - aiohttp cleanup hooks use an async callback contract.
    app: web.Application,
) -> None:
    ingress = app[webhook_ingress_key]
    dispatcher = cast("WebhookConversationDispatcher", ingress.conversation_dispatcher)
    dispatcher.close()


async def _dispatch_admitted_event(
    ingress: WebhookIngress,
    event: WebhookEvent,
    admission: WebhookAdmission,
    workspace: WorkspaceProfile | None,
    conversation_id: ConversationId | None,
) -> None:
    dispatcher = ingress.conversation_dispatcher
    if conversation_id is not None:
        await cast("WebhookConversationDispatcher", dispatcher).wake(conversation_id)
    if admission.created and admission.task is not None:
        ingress.deps.dispatch_scheduled_task(admission.task)
    if admission.created and event.host_message is not None and workspace is not None:
        notification_jid = (
            event.conversation.notification_jid if event.conversation is not None else None
        )
        await ingress.deps.broadcast_host_message(
            notification_jid or workspace.jid,
            event.host_message,
        )


async def handle_webhook(request: web.Request) -> web.Response:
    """Authenticate, deduplicate, persist, and dispatch one provider delivery."""
    ingress = request.app[webhook_ingress_key]
    route = ingress.routes[request.path]
    rate_response = _rate_limit_response(request, ingress, route)
    if rate_response is not None:
        return rate_response
    body_result = await _request_body(request, route)
    if isinstance(body_result, web.Response):
        return body_result
    raw_body = body_result[0]

    received_at = datetime.now(UTC)
    prepared = await _prepared_event_and_workspace(
        request,
        ingress,
        route,
        body_result,
        received_at,
    )
    if isinstance(prepared, web.Response):
        return prepared
    event, workspace = prepared

    callback_decision = await effect_callback_decision(event)
    defer_process_event = (
        callback_decision is WebhookEffectCallbackDecision.HELD and route.process_event is not None
    )
    if callback_decision is WebhookEffectCallbackDecision.UNRELATED:
        processed_event = await _process_route_event(route, event)
        if isinstance(processed_event, web.Response):
            return processed_event
        event = processed_event

    received_at_text = received_at.isoformat()
    task = _task_for_event(route, event, workspace, received_at_text)
    disposition: Literal["accepted", "routed", "lifecycle", "notified", "ignored"] = "ignored"
    if task is not None:
        disposition = "accepted"
    elif event.ignored_reason is not None:
        disposition = "ignored"
    elif event.lifecycle is not None:
        disposition = "lifecycle"
    elif event.host_message is not None:
        disposition = "notified"
    else:
        disposition = "routed"
    receipt = WebhookReceipt(
        provider=route.provider,
        route=route.name,
        delivery_id=event.delivery_id,
        workspace=workspace.folder if workspace is not None else "unrouted",
        event_type=event.event_type,
        event_action=event.action,
        subject_id=event.subject_id,
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        disposition=disposition,
        ignored_reason=event.ignored_reason,
        task_id=task.id if task is not None else None,
        occurred_at=event.occurred_at,
        received_at=received_at_text,
    )
    admission, conversation_id = await admit_prepared_event(
        ingress.conversation_dispatcher,
        route,
        event,
        WebhookDeliveryAdmissionRequest(
            receipt=receipt,
            task=task,
            defer_process_event=defer_process_event,
        ),
    )
    await _dispatch_admitted_event(
        ingress,
        event,
        admission,
        workspace,
        conversation_id,
    )
    logger.info(
        "Webhook delivery admitted",
        provider=route.provider,
        route=route.name,
        delivery_id=event.delivery_id,
        disposition=admission.receipt.disposition,
        outbound_effect_held=admission.outbound_effect_held,
        outbound_effect_suppressed=admission.outbound_effect_suppressed,
        duplicate=not admission.created,
    )
    return web.json_response(
        {
            "status": (
                "ignored"
                if admission.outbound_effect_suppressed
                else "accepted"
                if admission.receipt.disposition in {"routed", "lifecycle"}
                else admission.receipt.disposition
            ),
            "duplicate": not admission.created,
        }
    )


def install_webhook_ingress(app: web.Application, ingress: WebhookIngress) -> None:
    """Register the validated ingress and its exact public POST paths."""
    app[webhook_ingress_key] = ingress
    if ingress.conversation_dispatcher is not None:
        app.on_startup.append(_prepare_conversation_routes)
        app.on_cleanup.append(_stop_conversation_routes)
    for path in ingress.routes:
        app.router.add_post(path, handle_webhook)
