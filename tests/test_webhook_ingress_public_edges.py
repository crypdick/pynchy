"""Public HTTP behavior for provider-authenticated webhook ingress."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import LinearWebhookHarness, public_runtime

from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.webhook_ingress import build_webhook_ingress
from pynchy.plugins.api import (
    WebhookAuthenticationError,
    WebhookEvent,
    WebhookPayloadError,
    WebhookProcessingError,
    WebhookRoute,
)
from pynchy.plugins.webhooks import WebhookConfigurationError

pytest_plugins = ("tests.webhook_lifecycle_support",)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


_SECRET_ENV = "WEBHOOK_EDGE_SECRET"  # pragma: allowlist secret  # noqa: S105 - environment variable name, not a credential value.
_NOW = datetime(2026, 7, 30, tzinfo=UTC)


class _IngressHarness(LinearWebhookHarness):
    def __init__(self) -> None:
        super().__init__()
        self.broadcasts: list[tuple[str, str]] = []
        self.capability_status_operations = Mock()
        self.deploy_operations = Mock()
        self.get_canary_report = AsyncMock(return_value={"scenarios": []})
        self.canary_run_to_dict = Mock(return_value={})
        self.work_item_execution_to_dict = Mock(return_value={})

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.broadcasts.append((jid, text))


def _event(
    delivery_id: str,
    *,
    instructions: str | None = "Run the approved webhook task",
    external_context: Mapping[str, object] | None = None,
    ignored_reason: str | None = None,
    host_message: str | None = None,
) -> WebhookEvent:
    return WebhookEvent(
        delivery_id=delivery_id,
        event_type="test",
        action="created",
        subject_id=delivery_id,
        occurred_at=_NOW.isoformat(),
        instructions=instructions,
        external_context=(external_context if external_context is not None else {"id": delivery_id})
        if instructions is not None
        else None,
        ignored_reason=ignored_reason,
        host_message=host_message,
    )


def _route(
    parser: Callable[[bytes, Mapping[str, str], str, datetime], WebhookEvent],
    **kwargs: Any,
) -> WebhookRoute:
    return WebhookRoute(
        provider="edge-test",
        name="events",
        workspace=kwargs.pop("workspace", "project"),
        secret_env=_SECRET_ENV,
        parse=parser,
        **kwargs,
    )


def _parser(event: WebhookEvent) -> Callable[..., WebhookEvent]:
    def parse(
        _raw_body: bytes,
        _headers: Mapping[str, str],
        _secret: str,
        _received_at: datetime,
    ) -> WebhookEvent:
        return event

    return parse


async def _post(
    monkeypatch: pytest.MonkeyPatch,
    route: WebhookRoute,
    *,
    body: bytes = b"delivery",
    content_type: str = "application/json",
) -> tuple[int, dict[str, object], LinearWebhookHarness]:
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    harness = _IngressHarness()
    client = TestClient(
        TestServer(create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,)))
    )
    await client.start_server()
    try:
        response = await client.post(
            route.path,
            data=body,
            headers={"Content-Type": content_type},
        )
        return response.status, await response.json(), harness
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_content_type_and_oversized_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status, body, _ = await _post(
        monkeypatch,
        _route(_parser(_event("content-type"))),
        content_type="text/plain",
    )
    assert (status, body) == (415, {"error": "application/json required"})

    status, body, _ = await _post(
        monkeypatch,
        _route(_parser(_event("too-large")), max_body_bytes=3),
        body=b"1234",
    )
    assert (status, body) == (413, {"error": "payload too large"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "body"),
    [
        (WebhookAuthenticationError("bad signature"), 401, {"error": "authentication failed"}),
        (WebhookPayloadError("bad payload"), 400, {"error": "invalid payload"}),
    ],
)
async def test_webhook_translates_provider_parse_failures(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: int,
    body: dict[str, object],
) -> None:
    def parse(
        _raw_body: bytes,
        _headers: Mapping[str, str],
        _secret: str,
        _received_at: datetime,
    ) -> WebhookEvent:
        raise error

    actual_status, actual_body, _ = await _post(monkeypatch, _route(parse))

    assert (actual_status, actual_body) == (status, body)


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", ["prepare_event", "process_event"])
async def test_webhook_returns_503_for_route_processing_failures(
    monkeypatch: pytest.MonkeyPatch,
    callback: str,
) -> None:
    fail = AsyncMock(side_effect=WebhookProcessingError("provider processing unavailable"))

    route = _route(_parser(_event("processing-failure")), **{callback: fail})
    status, body, _ = await _post(monkeypatch, route)

    assert (status, body) == (503, {"error": "webhook processing failed"})


@pytest.mark.asyncio
async def test_webhook_reports_unavailable_when_startup_secret_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    harness = _IngressHarness()
    route = _route(_parser(_event("secret-disappeared")))
    client = TestClient(
        TestServer(create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,)))
    )
    await client.start_server()
    monkeypatch.delenv(_SECRET_ENV)
    try:
        response = await client.post(
            route.path,
            data=b"delivery",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 503
        assert await response.json() == {"error": "webhook unavailable"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_rate_limit_applies_before_provider_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(_parser(_event("rate-limited")), rate_limit_requests=1)
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    harness = _IngressHarness()
    client = TestClient(
        TestServer(create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,)))
    )
    await client.start_server()
    try:
        first = await client.post(
            route.path, data=b"one", headers={"Content-Type": "application/json"}
        )
        second = await client.post(
            route.path, data=b"two", headers={"Content-Type": "application/json"}
        )
        assert first.status == 200
        assert second.status == 429
        assert await second.json() == {"error": "rate limit exceeded"}
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "expected_status"),
    [
        (
            _event("ignored", instructions=None, external_context=None, ignored_reason="filtered"),
            "ignored",
        ),
        (
            _event(
                "notified",
                instructions=None,
                external_context=None,
                host_message="Webhook received",
            ),
            "notified",
        ),
    ],
)
async def test_webhook_returns_the_event_disposition_and_dispatches_notifications(
    monkeypatch: pytest.MonkeyPatch,
    event: WebhookEvent,
    expected_status: str,
) -> None:
    status, body, harness = await _post(monkeypatch, _route(_parser(event)))

    assert status == 200
    assert body == {"status": expected_status, "duplicate": False}
    if expected_status == "notified":
        assert harness.broadcasts == [(harness.workspace.jid, "Webhook received")]
    else:
        assert not harness.broadcasts


def test_webhook_ingress_rejects_unknown_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    with pytest.raises(WebhookConfigurationError, match="unknown workspace"):
        build_webhook_ingress(
            _IngressHarness(),
            (_route(_parser(_event("unknown")), workspace="missing"),),
        )
