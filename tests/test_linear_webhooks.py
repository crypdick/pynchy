"""Behavioral coverage for authenticated Linear webhook task admission."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.host.orchestrator.http_control import (
    ControlPlaneRuntime,
    ControlPlaneToken,
    RequestRateLimiter,
)
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.plugins.integrations.linear_webhooks import (
    LinearWebhookRouteConfig,
    parse_linear_webhook,
)
from pynchy.plugins.webhooks import (
    WebhookAuthenticationError,
    WebhookConfigurationError,
    WebhookRoute,
)
from pynchy.state import get_all_tasks, get_webhook_receipt, init_test_database
from pynchy.types import ScheduledTask, WorkspaceProfile

_SIGNING_KEY = "linear-webhook-test-signing-key-long-enough"
_DELIVERY_ID = "234d1a4e-b617-4388-90fe-adc3633d6b72"
_ORGANIZATION_ID = "org-1"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _payload(
    *,
    now: datetime,
    event_type: str = "Comment",
    action: str = "create",
    data: dict[str, Any] | None = None,
    updated_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "actor": {"id": "user-1", "type": "user", "name": "Example User"},
        "data": data
        or {
            "id": "comment-1",
            "issueId": "issue-1",
            "body": "please review this",
        },
        "type": event_type,
        "url": "https://linear.app/acme/issue/PYN-1#comment-comment-1",
        "createdAt": now.isoformat(),
        "organizationId": _ORGANIZATION_ID,
        "webhookTimestamp": int(now.timestamp() * 1000),
    }
    if updated_from is not None:
        payload["updatedFrom"] = updated_from
    return payload


def _signed_request(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    raw_body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(_SIGNING_KEY.encode(), raw_body, hashlib.sha256).hexdigest()
    return raw_body, {
        "Content-Type": "application/json",
        "Linear-Delivery": _DELIVERY_ID,
        "Linear-Event": str(payload["type"]),
        "Linear-Signature": signature,
        "Linear-Timestamp": str(payload["webhookTimestamp"]),
    }


def _config() -> LinearWebhookRouteConfig:
    return LinearWebhookRouteConfig(
        name="project",
        workspace="project",
        organization_id=_ORGANIZATION_ID,
    )


def _route() -> WebhookRoute:
    config = _config()
    return WebhookRoute(
        provider="linear",
        name=config.name,
        workspace=config.workspace,
        secret_env=config.secret_env,
        parse=partial(parse_linear_webhook, config=config),
    )


@pytest.mark.parametrize("action", ["create", "update", "remove"])
def test_every_comment_change_maps_to_fenced_public_source_task(action: str) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now, action=action))

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.subject_id == "issue-1"
    assert event.instructions is not None
    assert "linear_get_issue" in event.instructions
    assert "linear_list_todos" in event.instructions
    assert "linear_submit_plan" in event.instructions
    assert "linear_claim_work_item" in event.instructions
    assert "does not grant execution authority" in event.instructions
    assert event.external_context is not None
    assert event.external_context["action"] == action
    assert event.external_context["comment_body"] == "please review this"


@pytest.mark.parametrize(
    ("action", "updated_from"),
    [("create", None), ("update", {"title": "Old title"}), ("remove", None)],
)
def test_every_issue_change_triggers_a_task(
    action: str,
    updated_from: dict[str, Any] | None,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action=action,
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Webhook callbacks",
                "state": {"id": "state-1", "name": "In Progress"},
            },
            updated_from=updated_from,
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is not None
    assert event.external_context is not None
    assert event.external_context["action"] == action
    assert event.external_context["updated_fields"] == (
        ["title"] if updated_from is not None else []
    )


def test_non_issue_or_comment_delivery_remains_durably_ignorable() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Project",
            data={"id": "project-1", "name": "Project"},
        )
    )

    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())

    assert event.instructions is None
    assert event.ignored_reason == "event_type_is_not_configured"


def test_invalid_signature_and_stale_timestamp_fail_before_parsing() -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(_payload(now=now))
    bad_headers = {**headers, "Linear-Signature": "0" * 64}

    with pytest.raises(WebhookAuthenticationError, match="signature"):
        parse_linear_webhook(raw_body, bad_headers, _SIGNING_KEY, now, config=_config())

    with pytest.raises(WebhookAuthenticationError, match="replay window"):
        parse_linear_webhook(
            raw_body,
            headers,
            _SIGNING_KEY,
            now + timedelta(minutes=2),
            config=_config(),
        )


class _WebhookDeps:
    def __init__(self, *, admin: bool = False) -> None:
        self.workspace = WorkspaceProfile(
            jid="linear:project",
            name="Project",
            folder="project",
            trigger="@Pynchy",
            is_admin=admin,
        )
        self.dispatched: list[ScheduledTask] = []

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        del jid, text

    def admin_chat_jid(self) -> str:
        return "admin"

    def get_plugin_manager(self) -> object:
        return object()

    def get_workspace(self, folder: str) -> WorkspaceProfile | None:
        return self.workspace if folder == self.workspace.folder else None

    def dispatch_scheduled_task(self, task: ScheduledTask) -> None:
        self.dispatched.append(task)


def _public_runtime() -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        bind_host="0.0.0.0",  # noqa: S104, RUF100 - exercise public-bind auth policy
        port=8484,
        unix_socket=None,
        public_bind=True,
        remote_auth_required=True,
        allow_remote_deploy=False,
        auth_token=ControlPlaneToken("control-plane-token-that-is-long-enough"),
        rate_limiter=RequestRateLimiter(request_limit=100, window_seconds=60),
    )


async def _post_linear_event(
    client: TestClient,
    payload: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    raw_body, headers = _signed_request(payload)
    response = await client.post("/webhooks/linear/project", data=raw_body, headers=headers)
    return response.status, await response.json()


async def test_signed_delivery_bypasses_bearer_and_creates_one_durable_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        now = datetime.now(UTC)
        first_status, first = await _post_linear_event(client, _payload(now=now))
        second_status, second = await _post_linear_event(client, _payload(now=now))
    finally:
        await client.close()

    assert first_status == second_status == 200
    assert first == {"status": "accepted", "duplicate": False}
    assert second == {"status": "accepted", "duplicate": True}
    tasks = await get_all_tasks()
    assert len(tasks) == 1
    assert tasks[0].input_source == "webhook:linear"
    assert tasks[0].context_mode == "isolated"
    assert "EXTERNAL_UNTRUSTED_CONTENT" in tasks[0].prompt
    assert len(deps.dispatched) == 1
    receipt = await get_webhook_receipt("linear", "project", _DELIVERY_ID)
    assert receipt is not None
    assert receipt.task_id == tasks[0].id


async def test_ignored_delivery_records_receipt_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", _SIGNING_KEY)
    deps = _WebhookDeps()
    app = create_http_app(deps, runtime=_public_runtime(), webhook_routes=(_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        payload = _payload(
            now=datetime.now(UTC),
            event_type="Project",
            data={"id": "project-1", "name": "Project"},
        )
        status, body = await _post_linear_event(client, payload)
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "ignored", "duplicate": False}
    assert not deps.dispatched
    assert not await get_all_tasks()


def test_route_refuses_admin_workspace() -> None:
    with pytest.raises(WebhookConfigurationError, match="cannot target admin"):
        create_http_app(
            _WebhookDeps(admin=True),
            runtime=_public_runtime(),
            webhook_routes=(_route(),),
        )


def test_route_requires_its_signing_secret_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LINEAR_WEBHOOK_SECRET", raising=False)

    with pytest.raises(WebhookConfigurationError, match="requires environment variable"):
        create_http_app(
            _WebhookDeps(),
            runtime=_public_runtime(),
            webhook_routes=(_route(),),
        )
