"""Authenticated plugin webhook ingress and durable task admission."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from aiohttp import web

from pynchy.host.container_manager.security.fencing import fence_untrusted_content
from pynchy.host.orchestrator.http_control import ClientAddress, RequestRateLimiter
from pynchy.host.orchestrator.webhook_conversations import (
    ConversationWebhookDeps,
    WebhookConversationDispatcher,
)
from pynchy.logger import logger
from pynchy.plugins.webhooks import (
    WebhookAuthenticationError,
    WebhookConfigurationError,
    WebhookEvent,
    WebhookPayloadError,
    WebhookProcessingError,
    WebhookRoute,
    validate_webhook_routes,
)
from pynchy.state import (
    WebhookAdmission,
    WebhookReceipt,
    admit_webhook_receipt,
    get_webhook_receipt,
)
from pynchy.types import ScheduledTask, WorkspaceProfile


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


def build_webhook_ingress(
    deps: WebhookIngressDeps,
    routes: tuple[WebhookRoute, ...],
) -> WebhookIngress:
    """Validate and assemble one host-owned ingress registry."""
    # NOTE: Update docs/usage/control-plane.md "Provider-authenticated webhooks"
    # and docs/architecture/security.md "HTTP Control Plane" with this boundary.
    for route in routes:
        workspace = deps.get_workspace(route.workspace)
        if workspace is None:
            raise WebhookConfigurationError(
                f"Webhook route {route.path} names unknown workspace {route.workspace!r}"
            )
        if workspace.is_admin:
            raise WebhookConfigurationError(
                f"Webhook route {route.path} cannot target admin workspace {route.workspace!r}"
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


async def _process_route_event(
    route: WebhookRoute,
    event: WebhookEvent,
) -> web.Response | None:
    """Apply one route-owned trusted host effect once before receipt admission."""
    existing = await get_webhook_receipt(route.provider, route.name, event.delivery_id)
    if existing is not None:
        return None
    if route.process_event is None:
        return None
    try:
        await route.process_event(event)
    except WebhookProcessingError as exc:
        logger.warning(
            "Authenticated webhook host effect failed",
            route=route.path,
            reason=str(exc),
        )
        return web.json_response({"error": "webhook processing failed"}, status=503)
    return None


def _parse_event(
    request: web.Request,
    route: WebhookRoute,
    raw_body: bytes,
    secret: str,
    received_at: datetime,
) -> WebhookEvent | web.Response:
    try:
        return route.parse(raw_body, dict(request.headers), secret, received_at)
    except WebhookAuthenticationError as exc:
        logger.warning("Webhook authentication failed", route=route.path, reason=str(exc))
        return web.json_response({"error": "authentication failed"}, status=401)
    except WebhookPayloadError as exc:
        logger.warning("Authenticated webhook payload rejected", route=route.path, reason=str(exc))
        return web.json_response({"error": "invalid payload"}, status=400)


def _task_for_event(
    route: WebhookRoute,
    event: WebhookEvent,
    workspace: WorkspaceProfile,
    received_at: str,
) -> ScheduledTask | None:
    if (
        event.instructions is None
        or event.external_context is None
        or event.conversation is not None
    ):
        return None
    return ScheduledTask(
        id=f"webhook-{route.provider}-{route.name}-{event.delivery_id}",
        group_folder=workspace.folder,
        chat_jid=workspace.jid,
        prompt=_prompt_for_event(route, event),
        schedule_type="once",
        schedule_value=received_at,
        context_mode="isolated",
        next_run=received_at,
        created_at=received_at,
        input_source=f"webhook:{route.provider}",
    )


def _prompt_for_event(route: WebhookRoute, event: WebhookEvent) -> str:
    if event.instructions is None or event.external_context is None:
        raise ValueError("Actionable webhook event lost its prompt context")
    external_context = fence_untrusted_content(
        json.dumps(event.external_context, sort_keys=True, ensure_ascii=False),
        source=f"{route.provider}-webhook",
    )
    return f"{event.instructions}\n\n{external_context}"


async def _start_conversation_routes(app: web.Application) -> None:
    ingress = app[webhook_ingress_key]
    if ingress.conversation_dispatcher is not None:
        await ingress.conversation_dispatcher.start()


async def _stop_conversation_routes(  # noqa: RUF029, RUF100 - aiohttp cleanup hooks use an async callback contract.
    app: web.Application,
) -> None:
    ingress = app[webhook_ingress_key]
    if ingress.conversation_dispatcher is not None:
        ingress.conversation_dispatcher.close()


async def _dispatch_admitted_event(
    ingress: WebhookIngress,
    route: WebhookRoute,
    event: WebhookEvent,
    admission: WebhookAdmission,
    workspace: WorkspaceProfile,
) -> None:
    dispatcher = ingress.conversation_dispatcher
    if event.conversation is not None:
        if dispatcher is None:
            raise RuntimeError("Routed webhook dispatcher disappeared after startup")
        conversation_id = await dispatcher.admit(
            route,
            event,
            _prompt_for_event(route, event),
        )
        if conversation_id is not None:
            await dispatcher.wake(conversation_id)
    if admission.created and admission.task is not None:
        ingress.deps.dispatch_scheduled_task(admission.task)
    if admission.created and event.host_message is not None:
        await ingress.deps.broadcast_host_message(workspace.jid, event.host_message)


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
    raw_body, secret = body_result

    received_at = datetime.now(UTC)
    event = _parse_event(request, route, raw_body, secret, received_at)
    if isinstance(event, web.Response):
        return event
    workspace = ingress.deps.get_workspace(route.workspace)
    if workspace is None or workspace.is_admin:
        logger.error("Webhook route lost its configured non-admin workspace", route=route.path)
        return web.json_response({"error": "webhook route unavailable"}, status=503)

    processing_response = await _process_route_event(route, event)
    if processing_response is not None:
        return processing_response

    received_at_text = received_at.isoformat()
    task = _task_for_event(route, event, workspace, received_at_text)
    disposition: Literal["accepted", "routed", "notified", "ignored"]
    if task is not None:
        disposition = "accepted"
    elif event.conversation is not None:
        disposition = "routed"
    elif event.host_message is not None:
        disposition = "notified"
    else:
        disposition = "ignored"
    receipt = WebhookReceipt(
        provider=route.provider,
        route=route.name,
        delivery_id=event.delivery_id,
        workspace=route.workspace,
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
    admission = await admit_webhook_receipt(receipt, task)
    await _dispatch_admitted_event(ingress, route, event, admission, workspace)
    logger.info(
        "Webhook delivery admitted",
        provider=route.provider,
        route=route.name,
        delivery_id=event.delivery_id,
        disposition=admission.receipt.disposition,
        duplicate=not admission.created,
    )
    return web.json_response(
        {
            "status": (
                "accepted"
                if admission.receipt.disposition == "routed"
                else admission.receipt.disposition
            ),
            "duplicate": not admission.created,
        }
    )


def install_webhook_ingress(app: web.Application, ingress: WebhookIngress) -> None:
    """Register the validated ingress and its exact public POST paths."""
    app[webhook_ingress_key] = ingress
    if ingress.conversation_dispatcher is not None:
        app.on_startup.append(_start_conversation_routes)
        app.on_cleanup.append(_stop_conversation_routes)
    for path in ingress.routes:
        app.router.add_post(path, handle_webhook)
