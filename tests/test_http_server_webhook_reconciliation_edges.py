"""Public behavior at the HTTP webhook reconciliation boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_http_server import MockHttpDeps

from pynchy.conversation_primitives import (
    ConversationDeliveryCompletion,
    ConversationId,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.http_control import ControlPlaneRuntime, RequestRateLimiter
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.webhook_effects import WebhookEffectResolution


def runtime() -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        bind_host="127.0.0.1",
        port=8484,
        unix_socket=None,
        public_bind=False,
        remote_auth_required=False,
        allow_remote_deploy=False,
        auth_token=None,
        rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        audit_security_event=AsyncMock(),
    )


class _NoRuntimeIngress:
    def __init__(self) -> None:
        delegate = MockHttpDeps()
        self.broadcast_synthetic_user_input = delegate.broadcast_synthetic_user_input
        self.capability_status_operations = delegate.capability_status_operations
        self.deploy_operations = delegate.deploy_operations
        self.canary_run_to_dict = delegate.canary_run_to_dict
        self.work_item_execution_to_dict = delegate.work_item_execution_to_dict

    async def broadcast_host_message(self, _jid: str, _text: str) -> None: ...

    def admin_chat_jid(self) -> str:
        return "admin"

    async def get_canary_report(self, *, history_limit: int) -> dict[str, object]:
        return {}

    def get_workspace(self, _folder: str) -> None:
        return None

    def dispatch_scheduled_task(self, _task: object) -> None: ...


def test_runtime_harness_requires_message_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYNCHY_RUNTIME_HARNESS", "1")

    with pytest.raises(TypeError, match="do not provide message ingress"):
        create_http_app(_NoRuntimeIngress(), runtime=runtime())


@pytest.mark.asyncio
async def test_webhook_effect_absence_wakes_released_deliveries() -> None:
    client = TestClient(TestServer(create_http_app(MockHttpDeps(), runtime=runtime())))
    await client.start_server()
    completion = ConversationDeliveryCompletion(
        identity=ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId("delivery-1"),
        ),
        conversation_id=ConversationId("conversation-1"),
    )
    try:
        with (
            patch(
                "pynchy.host.orchestrator.http_server.reconcile_webhook_effect_absent",
                new=AsyncMock(return_value=WebhookEffectResolution((completion,))),
            ),
            patch(
                "pynchy.host.orchestrator.http_server.notify_conversation_delivery_completed",
                new=AsyncMock(),
            ) as notify,
        ):
            response = await client.post(
                "/webhook-effects/effect-1/reconcile-absent",
                json={"verified_absent": True},
            )

        assert response.status == 200
        assert await response.json() == {
            "status": "reconciled_absent",
            "released_deliveries": 1,
        }
        notify.assert_awaited_once_with(completion)
    finally:
        await client.close()
