"""HTTP ingress coverage for callback-first Linear Issue/update correlation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import (
    SIGNING_KEY,
    LinearWebhookHarness,
    payload,
    post_linear_event,
    public_runtime,
    route_config,
    webhook_route,
)

from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.plugins.integrations.linear_self_echoes import linear_self_echo_recorder
from pynchy.plugins.integrations.linear_webhook_evidence import issue_state_webhook_evidence
from pynchy.state import init_test_database
from pynchy.webhook_effects import WebhookEffectScope

_ISSUE_ID = "issue-1"
_STATE_ID = "state-awaiting-review"
_REVISION = "2026-07-26T16:00:01+00:00"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _issue_payload(
    now: datetime,
    *,
    state_id: str = _STATE_ID,
    state_type: str = "started",
) -> dict[str, object]:
    return payload(
        now=now,
        event_type="Issue",
        action="update",
        data={
            "id": _ISSUE_ID,
            "identifier": "PYN-1",
            "title": "Pynchy state update",
            "state": {
                "id": state_id,
                "name": "Awaiting Review",
                "type": state_type,
            },
            "updatedAt": _REVISION,
        },
        updated_from={"stateId": "state-in-progress"},
    )


async def _start_effect():
    recorder = linear_self_echo_recorder(route_config().tool)
    scope = WebhookEffectScope(
        provider="linear",
        account=route_config().tool,
        event_type="Issue",
        event_action="update",
        subject_id=_ISSUE_ID,
    )
    effect_id = await recorder.begin(scope)
    await recorder.mark_executing(effect_id)
    return recorder, effect_id


async def test_state_callback_before_response_is_completed_without_agent_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    recorder, effect_id = await _start_effect()
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, body = await post_linear_event(client, _issue_payload(datetime.now(UTC)))
        assert status == 200
        assert body == {"status": "accepted", "duplicate": False}
        assert harness.ingested == []

        await recorder.confirm(
            effect_id,
            issue_state_webhook_evidence(
                route_config().tool,
                issue_id=_ISSUE_ID,
                state_id=_STATE_ID,
                revision=_REVISION,
            ),
        )
    finally:
        await client.close()

    assert harness.ingested == []


async def test_terminal_state_callback_is_not_suppressed_by_nonterminal_effect_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    await _start_effect()
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, body = await post_linear_event(
            client,
            _issue_payload(
                datetime.now(UTC),
                state_id="state-done",
                state_type="completed",
            ),
        )
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "accepted", "duplicate": False}
    assert harness.ingested == []
