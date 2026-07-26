"""HTTP ingress coverage for Pynchy-authored Linear Issue state updates."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import (
    DELIVERY_ID,
    SECOND_DELIVERY_ID,
    SIGNING_KEY,
    THIRD_DELIVERY_ID,
    LinearWebhookHarness,
    payload,
    post_linear_event,
    public_runtime,
    route_config,
    webhook_route,
)

from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.state import (
    LinearIssueStateSelfEcho,
    WebhookReceipt,
    admit_webhook_receipt,
    get_webhook_receipt,
    init_test_database,
    record_linear_issue_state_self_echo,
)

_ISSUE_ID = "issue-1"
_STATE_ID = "state-awaiting-review"
_REVISION = "2026-07-26T04:45:00+00:00"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _issue_payload(
    now: datetime,
    *,
    state_id: str = _STATE_ID,
    state_type: str = "started",
    revision: str = _REVISION,
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
            "updatedAt": revision,
        },
        updated_from={"stateId": "state-in-progress"},
    )


def _marker(
    *,
    state_id: str = _STATE_ID,
    revision: str = _REVISION,
) -> LinearIssueStateSelfEcho:
    return LinearIssueStateSelfEcho(
        account_name=route_config().tool,
        issue_id=_ISSUE_ID,
        state_id=state_id,
        revision=revision,
    )


async def test_exact_nonterminal_state_echo_is_ignored_once_and_retry_stays_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    await record_linear_issue_state_self_echo(_marker())
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        event = _issue_payload(datetime.now(UTC))
        first_status, first = await post_linear_event(client, event)
        retry_status, retry = await post_linear_event(client, event)
        consumed_status, consumed = await post_linear_event(
            client,
            event,
            delivery_id=SECOND_DELIVERY_ID,
        )
    finally:
        await client.close()

    assert first_status == retry_status == consumed_status == 200
    assert first == {"status": "ignored", "duplicate": False}
    assert retry == {"status": "ignored", "duplicate": True}
    assert consumed == {"status": "accepted", "duplicate": False}
    assert len(harness.ingested) == 1
    assert len(harness.channel.created) == 1

    receipt = await get_webhook_receipt("linear", "project", DELIVERY_ID)
    assert receipt is not None
    assert receipt.disposition == "ignored"
    assert receipt.ignored_reason == "pynchy_self_issue_state_echo"


async def test_state_callback_without_an_exact_marker_remains_actionable_regardless_of_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        event = _issue_payload(datetime.now(UTC))
        event["actor"] = {
            "id": "pynchy-shared-linear-account",
            "type": "user",
            "name": "Pynchy account",
        }
        status, body = await post_linear_event(client, event)
    finally:
        await client.close()

    assert status == 200
    assert body == {"status": "accepted", "duplicate": False}
    assert len(harness.ingested) == 1
    assert "Event: issue update" in harness.ingested[0].content


async def test_mismatched_state_revision_routes_and_preserves_exact_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    await record_linear_issue_state_self_echo(_marker())
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        now = datetime.now(UTC)
        mismatch_status, mismatch = await post_linear_event(
            client,
            _issue_payload(now, revision="2026-07-26T04:45:01+00:00"),
        )
        exact_status, exact = await post_linear_event(
            client,
            _issue_payload(now),
            delivery_id=THIRD_DELIVERY_ID,
        )
    finally:
        await client.close()

    assert mismatch_status == exact_status == 200
    assert mismatch == {"status": "accepted", "duplicate": False}
    assert exact == {"status": "ignored", "duplicate": False}
    assert len(harness.ingested) == 1


async def test_terminal_state_callback_keeps_its_lifecycle_effect_when_a_marker_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    marker = _marker(state_id="state-done")
    await record_linear_issue_state_self_echo(marker)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
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
    receipt = await get_webhook_receipt("linear", "project", DELIVERY_ID)
    assert receipt is not None
    assert receipt.disposition == "lifecycle"

    preserved = await admit_webhook_receipt(
        WebhookReceipt(
            provider="linear",
            route="project",
            delivery_id=THIRD_DELIVERY_ID,
            workspace="project",
            event_type="Issue",
            event_action="update",
            subject_id=_ISSUE_ID,
            payload_sha256="preserved-marker",
            disposition="routed",
            ignored_reason=None,
            task_id=None,
            occurred_at=_REVISION,
            received_at=_REVISION,
        ),
        None,
        self_echo=marker,
    )
    assert preserved.self_echo_suppressed is True
