"""Authenticated plugin webhook ingress and durable task admission."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from aiohttp import web

from pynchy.host.container_manager.security.fencing import fence_untrusted_content
from pynchy.host.orchestrator.http_control import ClientAddress, RequestRateLimiter
from pynchy.logger import logger
from pynchy.plugins.webhooks import (
    WebhookAuthenticationError,
    WebhookConfigurationError,
    WebhookEvent,
    WebhookPayloadError,
    WebhookRoute,
    validate_webhook_routes,
)
from pynchy.state import WebhookReceipt, admit_webhook_receipt
from pynchy.types import ScheduledTask, WorkspaceProfile


class WebhookIngressDeps(Protocol):
    """Host capabilities required by provider webhook ingress."""

    def get_workspace(self, folder: str) -> WorkspaceProfile | None: ...

    def dispatch_scheduled_task(self, task: ScheduledTask) -> None: ...


@dataclass(frozen=True)
class WebhookIngress:
    """Validated route registry installed into one aiohttp application."""

    deps: WebhookIngressDeps
    routes: dict[str, WebhookRoute]
    limiters: dict[str, RequestRateLimiter]

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
    validated = validate_webhook_routes(routes)
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
    if event.instructions is None or event.external_context is None:
        return None
    external_context = fence_untrusted_content(
        json.dumps(event.external_context, sort_keys=True, ensure_ascii=False),
        source=f"{route.provider}-webhook",
    )
    return ScheduledTask(
        id=f"webhook-{route.provider}-{route.name}-{event.delivery_id}",
        group_folder=workspace.folder,
        chat_jid=workspace.jid,
        prompt=f"{event.instructions}\n\n{external_context}",
        schedule_type="once",
        schedule_value=received_at,
        context_mode="isolated",
        next_run=received_at,
        created_at=received_at,
        input_source=f"webhook:{route.provider}",
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
    raw_body, secret = body_result

    received_at = datetime.now(UTC)
    event = _parse_event(request, route, raw_body, secret, received_at)
    if isinstance(event, web.Response):
        return event
    workspace = ingress.deps.get_workspace(route.workspace)
    if workspace is None or workspace.is_admin:
        logger.error("Webhook route lost its configured non-admin workspace", route=route.path)
        return web.json_response({"error": "webhook route unavailable"}, status=503)

    received_at_text = received_at.isoformat()
    task = _task_for_event(route, event, workspace, received_at_text)
    receipt = WebhookReceipt(
        provider=route.provider,
        route=route.name,
        delivery_id=event.delivery_id,
        workspace=route.workspace,
        event_type=event.event_type,
        event_action=event.action,
        subject_id=event.subject_id,
        payload_sha256=hashlib.sha256(raw_body).hexdigest(),
        disposition="accepted" if task is not None else "ignored",
        ignored_reason=event.ignored_reason,
        task_id=task.id if task is not None else None,
        occurred_at=event.occurred_at,
        received_at=received_at_text,
    )
    admission = await admit_webhook_receipt(receipt, task)
    if admission.created and admission.task is not None:
        ingress.deps.dispatch_scheduled_task(admission.task)
    logger.info(
        "Webhook delivery admitted",
        provider=route.provider,
        route=route.name,
        delivery_id=event.delivery_id,
        disposition=admission.receipt.disposition,
        duplicate=not admission.created,
    )
    return web.json_response(
        {"status": admission.receipt.disposition, "duplicate": not admission.created}
    )


def install_webhook_ingress(app: web.Application, ingress: WebhookIngress) -> None:
    """Register the validated ingress and its exact public POST paths."""
    app[webhook_ingress_key] = ingress
    for path in ingress.routes:
        app.router.add_post(path, handle_webhook)
