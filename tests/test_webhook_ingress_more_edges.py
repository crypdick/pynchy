"""Additional public behavior tests for webhook ingress validation and routing."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import public_runtime

from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.webhook_ingress import (
    build_webhook_ingress,
    recover_webhook_conversations,
)
from pynchy.plugins.api import WebhookEvent
from pynchy.plugins.webhooks import WebhookConfigurationError
from pynchy.state import init_test_database
from pynchy.webhook_effects import WebhookEffectCallbackDecision
from pynchy.workspace.api import WorkspaceProfile
from tests.test_webhook_ingress_public_edges import (
    _SECRET_ENV,
    _event,
    _IngressHarness,
    _parser,
    _post,
    _route,
)
from tests.webhook_lifecycle_support import _conversation

pytest_plugins = ("tests.webhook_lifecycle_support",)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _workspace(folder: str, *, is_admin: bool = False) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=f"discord:channel:{folder}",
        name=folder.title(),
        folder=folder,
        trigger="@Pynchy",
        is_admin=is_admin,
    )


class _BasicIngressDeps:
    def get_workspace(self, folder: str) -> WorkspaceProfile | None:
        return _workspace("project") if folder == "project" else None

    def dispatch_scheduled_task(self, _task) -> None:
        pass

    async def broadcast_host_message(self, _jid: str, _text: str) -> None:
        pass


def test_ingress_rejects_admin_targets_and_route_workspace_validators() -> None:
    deps = _IngressHarness()
    admin = _workspace("admin", is_admin=True)
    deps.workspace_map[admin.jid] = admin
    deps.get_workspace = lambda folder: deps.workspace_map.get(f"discord:channel:{folder}")

    with pytest.raises(WebhookConfigurationError, match="cannot target admin workspace"):
        build_webhook_ingress(deps, (_route(_parser(_event("admin")), workspace="admin"),))

    with pytest.raises(WebhookConfigurationError, match="is not allowed"):
        build_webhook_ingress(
            deps,
            (
                _route(
                    _parser(_event("validator")),
                    validate_workspace=lambda _workspace: "is not allowed",
                ),
            ),
        )


def test_ingress_requires_conversation_capabilities() -> None:
    with pytest.raises(
        WebhookConfigurationError, match="requires conversation runtime capabilities"
    ):
        build_webhook_ingress(
            _BasicIngressDeps(),
            (
                _route(
                    _parser(_event("conversation-capability")),
                    routes_conversations=True,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_ingress_rejects_a_runtime_workspace_that_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event("workspace-disappeared"),
        conversation=replace(_conversation(), workspace="missing"),
    )
    status, body, _ = await _post(monkeypatch, _route(_parser(event)))

    assert (status, body) == (503, {"error": "webhook route unavailable"})


@pytest.mark.asyncio
async def test_ingress_rejects_a_runtime_admin_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event("runtime-admin"),
        conversation=replace(_conversation(), workspace="admin"),
    )
    harness = _IngressHarness()
    admin = _workspace("admin", is_admin=True)
    harness.workspace_map[admin.jid] = admin
    monkeypatch.setattr(
        type(harness),
        "get_workspace",
        lambda self, folder: self.workspace_map.get(f"discord:channel:{folder}"),
    )
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    client = TestClient(
        TestServer(
            create_http_app(
                harness,
                runtime=public_runtime(),
                webhook_routes=(_route(_parser(event)),),
            )
        )
    )
    await client.start_server()
    try:
        response = await client.post(
            "/webhooks/edge-test/events",
            data=b"delivery",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 503
        assert await response.json() == {"error": "webhook route unavailable"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingress_rejects_a_resolved_but_undeclared_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = replace(
        _event("undeclared-workspace"),
        conversation=replace(_conversation(), workspace="other"),
    )
    harness = _IngressHarness()
    other = _workspace("other")
    harness.workspace_map[other.jid] = other
    monkeypatch.setattr(
        type(harness),
        "get_workspace",
        lambda self, folder: self.workspace_map.get(f"discord:channel:{folder}"),
    )
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    route = _route(_parser(event))
    client = TestClient(
        TestServer(create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,)))
    )
    await client.start_server()
    try:
        response = await client.post(
            route.path,
            data=b"delivery",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 503
        assert await response.json() == {"error": "webhook route unavailable"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingress_processes_unrelated_event_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event("processed-event")
    processed = WebhookEvent(
        delivery_id=event.delivery_id,
        event_type=event.event_type,
        action=event.action,
        subject_id=event.subject_id,
        occurred_at=event.occurred_at,
        instructions=None,
        external_context=None,
        host_message="Processed by provider effect",
    )
    process_event = AsyncMock(return_value=processed)
    route = _route(_parser(event), process_event=process_event)

    status, body, harness = await _post(monkeypatch, route)

    assert (status, body) == (200, {"status": "notified", "duplicate": False})
    process_event.assert_awaited_once_with(event)
    assert harness.broadcasts == [(harness.workspace.jid, processed.host_message)]


def test_ingress_accepts_a_workspace_validator_without_a_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    ingress = build_webhook_ingress(
        _IngressHarness(),
        (_route(_parser(_event("validator-accepted")), validate_workspace=lambda _: None),),
    )

    assert ingress.public_paths == frozenset({"/webhooks/edge-test/events"})


def test_ingress_rejects_unknown_workspace_without_conversation_capabilities() -> None:
    with pytest.raises(WebhookConfigurationError, match="unknown workspace"):
        build_webhook_ingress(
            _BasicIngressDeps(),
            (_route(_parser(_event("unknown-basic")), workspace="missing"),),
        )


@pytest.mark.asyncio
async def test_ingress_rejects_a_chunked_body_that_exceeds_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def chunks():  # noqa: RUF029 - aiohttp request body must be streamed asynchronously.
        yield b"12"
        yield b"34"

    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    harness = _IngressHarness()
    route = _route(_parser(_event("chunked-too-large")), max_body_bytes=3)
    client = TestClient(
        TestServer(create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,)))
    )
    await client.start_server()
    try:
        response = await client.post(
            route.path,
            data=chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 413
        assert await response.json() == {"error": "payload too large"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingress_handles_a_workspace_that_disappears_after_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    harness = _IngressHarness()
    route = _route(_parser(_event("workspace-lost")))
    client = TestClient(
        TestServer(create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,)))
    )
    await client.start_server()
    monkeypatch.setattr(type(harness), "get_workspace", lambda _self, _folder: None)
    harness.workspace_map.clear()
    try:
        response = await client.post(
            route.path,
            data=b"delivery",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 503
        assert await response.json() == {"error": "webhook route unavailable"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingress_skips_conversation_hooks_when_no_conversation_route_exists() -> None:
    harness = _IngressHarness()
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=())
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await recover_webhook_conversations(app)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingress_defers_a_held_effect_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _IngressHarness()
    route = _route(_parser(_event("held-effect")), process_event=AsyncMock())
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        monkeypatch.setattr(
            "pynchy.host.orchestrator.webhook_ingress.effect_callback_decision",
            AsyncMock(return_value=WebhookEffectCallbackDecision.HELD),
        )
        response = await client.post(
            route.path,
            data=b"delivery",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 200
        assert await response.json() == {"status": "accepted", "duplicate": False}
        route.process_event.assert_not_awaited()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ingress_rejects_an_actionable_event_without_a_default_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = _route(
        _parser(_event("missing-default-workspace")),
        workspace=None,
        candidate_workspaces=("project",),
    )
    monkeypatch.setenv(_SECRET_ENV, "edge-secret")  # pragma: allowlist secret
    harness = _IngressHarness()
    client = TestClient(
        TestServer(create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,)))
    )
    await client.start_server()
    try:
        response = await client.post(
            route.path,
            data=b"delivery",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 503
        assert await response.json() == {"error": "webhook route unavailable"}
    finally:
        await client.close()
