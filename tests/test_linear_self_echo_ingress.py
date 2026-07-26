"""HTTP ingress coverage for exact Pynchy-authored Linear comment echoes."""

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
    LinearCommentSelfEcho,
    get_webhook_receipt,
    init_test_database,
    record_linear_comment_self_echo,
)

_COMMENT_ID = "comment-1"
_ISSUE_ID = "issue-1"
_REVISION = "2026-07-26T04:45:00+00:00"


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _comment_payload(now: datetime, *, revision: str = _REVISION) -> dict[str, object]:
    return payload(
        now=now,
        data={
            "id": _COMMENT_ID,
            "issueId": _ISSUE_ID,
            "body": "Pynchy posted this update.",
            "updatedAt": revision,
        },
    )


def _marker() -> LinearCommentSelfEcho:
    return LinearCommentSelfEcho(
        account_name=route_config().tool,
        comment_id=_COMMENT_ID,
        issue_id=_ISSUE_ID,
        revision=_REVISION,
    )


async def test_exact_self_comment_echo_is_ignored_once_and_retry_remains_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    await record_linear_comment_self_echo(_marker())
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        event = _comment_payload(datetime.now(UTC))
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
    assert receipt.ignored_reason == "pynchy_self_comment_echo"


async def test_comment_without_exact_marker_remains_actionable_regardless_of_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        event = _comment_payload(datetime.now(UTC))
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
    assert "Pynchy posted this update." in harness.ingested[0].content

    receipt = await get_webhook_receipt("linear", "project", DELIVERY_ID)
    assert receipt is not None
    assert receipt.disposition == "routed"


async def test_mismatched_comment_revision_routes_and_preserves_exact_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", SIGNING_KEY)
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    await record_linear_comment_self_echo(_marker())
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(webhook_route(),))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        now = datetime.now(UTC)
        mismatch_status, mismatch = await post_linear_event(
            client,
            _comment_payload(now, revision="2026-07-26T04:45:01+00:00"),
        )
        exact_status, exact = await post_linear_event(
            client,
            _comment_payload(now),
            delivery_id=THIRD_DELIVERY_ID,
        )
    finally:
        await client.close()

    assert mismatch_status == exact_status == 200
    assert mismatch == {"status": "accepted", "duplicate": False}
    assert exact == {"status": "ignored", "duplicate": False}
    assert len(harness.ingested) == 1

    mismatch_receipt = await get_webhook_receipt("linear", "project", DELIVERY_ID)
    exact_receipt = await get_webhook_receipt("linear", "project", THIRD_DELIVERY_ID)
    assert mismatch_receipt is not None
    assert mismatch_receipt.disposition == "routed"
    assert exact_receipt is not None
    assert exact_receipt.ignored_reason == "pynchy_self_comment_echo"
