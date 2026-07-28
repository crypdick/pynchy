"""Business coverage for provider-neutral lifecycle-only webhook deliveries."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from aiohttp.test_utils import TestClient, TestServer
from linear_webhook_test_support import LinearWebhookHarness, public_runtime

from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationDeliveryStatus,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    TerminalConversationRetirement,
)
from pynchy.conversation.workspaces import dynamic_thread_folder, routed_conversation_folder
from pynchy.host.orchestrator.http_server import create_http_app
from pynchy.host.orchestrator.webhook_conversations import WebhookConversationDispatcher
from pynchy.state import (
    claim_next_conversation_delivery,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_delivery,
    get_webhook_receipt,
    prepare_conversation_delivery_recovery,
)
from tests.webhook_lifecycle_support import (
    _admit,
    _delivery_identity,
    _lifecycle_event,
    _lifecycle_parser,
    _message_event,
    _route,
)

pytest_plugins = ("tests.webhook_lifecycle_support",)

if TYPE_CHECKING:
    from pynchy.plugins.api import (
        WebhookLifecycleDelivery,
    )

_NOW = datetime(2026, 7, 26, tzinfo=UTC).isoformat()
_SUBJECT = ConversationSubject(
    namespace=ConversationSubjectNamespace("test-provider:tenant:issue"),
    key=ConversationSubjectKey("issue-1"),
)


async def test_startup_recovers_terminal_cleanup_after_ingress_state_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        conversation_id = await _admit(dispatcher, route, _message_event("before-terminal"))
        await dispatcher.wake(conversation_id)
        runtime_jid = harness.ingested[0].chat_jid
        assert runtime_jid in harness.workspace_map

        async def persist_terminal_state(  # noqa: RUF029 - runtime callback contract is async.
            deps: LinearWebhookHarness,
            terminal_conversation_id: ConversationId,
            retirement: TerminalConversationRetirement,
            runtime_workspace_folders: set[str],
        ) -> bool:
            del deps, terminal_conversation_id, retirement, runtime_workspace_folders
            return False

        monkeypatch.setattr(
            "pynchy.host.orchestrator.webhook_conversations.retire_terminal_runtime",
            persist_terminal_state,
        )
        await _admit(dispatcher, route, _lifecycle_event("terminal-after-crash"))

        terminal = await get_conversation(conversation_id)
        assert terminal is not None
        assert terminal.control_closed is True
        assert runtime_jid in harness.workspace_map
        assert harness.retired_folders == []
    finally:
        dispatcher.close()

    recovered = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await recovered.start()
    try:
        assert runtime_jid not in harness.workspace_map
        assert set(harness.retired_folders) == {
            routed_conversation_folder("project", conversation_id),
            dynamic_thread_folder("project", runtime_jid),
        }
        assert harness.retired_task_conversations == [conversation_id]
    finally:
        recovered.close()


async def test_lifecycle_delivery_retries_terminal_cleanup_after_ingress_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        conversation_id = await _admit(dispatcher, route, _message_event("retry-cleanup-base"))
        await dispatcher.wake(conversation_id)
        runtime_jid = harness.ingested[0].chat_jid
        original_retire_tasks = harness.retire_conversation_tasks
        attempts = 0

        async def fail_once(terminal_conversation_id: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("Temporal unavailable")
            await original_retire_tasks(terminal_conversation_id)

        monkeypatch.setattr(harness, "retire_conversation_tasks", fail_once)
        with pytest.raises(RuntimeError, match="Temporal unavailable"):
            await _admit(dispatcher, route, _lifecycle_event("retry-cleanup-terminal"))

        assert runtime_jid in harness.workspace_map
        await dispatcher.wake(conversation_id)

        assert attempts == 2
        assert runtime_jid not in harness.workspace_map
        assert harness.retired_task_conversations == [conversation_id]
    finally:
        dispatcher.close()


async def test_lifecycle_ingress_records_a_lifecycle_receipt_without_an_agent_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    seen_lifecycles: list[WebhookLifecycleDelivery] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        claimed = await get_conversation_delivery(delivery.identity)
        assert claimed is not None
        assert claimed.status is ConversationDeliveryStatus.CLAIMED
        seen_lifecycles.append(delivery)

    route = replace(_route(handle_lifecycle), parse=_lifecycle_parser)
    monkeypatch.setenv(route.secret_env, route.secret_env)
    app = create_http_app(harness, runtime=public_runtime(), webhook_routes=(route,))
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post(
            route.path,
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        body = await response.json()
        duplicate_response = await client.post(
            route.path,
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        duplicate = await duplicate_response.json()
    finally:
        await client.close()

    receipt = await get_webhook_receipt("test-provider", "project", "terminal-over-http")
    assert response.status == duplicate_response.status == 200
    assert body == {"status": "accepted", "duplicate": False}
    assert duplicate == {"status": "accepted", "duplicate": True}
    assert receipt is not None
    assert receipt.disposition == "lifecycle"
    assert len(seen_lifecycles) == 1
    assert not harness.ingested
    assert not harness.channel.created


async def test_lifecycle_callback_retry_keeps_the_original_control_close_transition() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    close_update_times: list[str] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        binding = await get_conversation_control_binding(delivery.conversation_id)
        assert binding is not None
        assert binding.closed is True
        close_update_times.append(binding.updated_at)
        if len(close_update_times) == 1:
            raise RuntimeError("retry the route-owned lifecycle effect")

    route = _route(handle_lifecycle)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        conversation_id = await _admit(dispatcher, route, _message_event("message-before-retry"))
        await dispatcher.wake(conversation_id)
        await _admit(dispatcher, route, _lifecycle_event("retry-after-close"))

        with pytest.raises(RuntimeError, match="retry the route-owned lifecycle effect"):
            await dispatcher.wake(conversation_id)
        await dispatcher.wake(conversation_id)

        assert len(close_update_times) == 2
        assert close_update_times[0] == close_update_times[1]
        assert len(harness.ingested) == 1
    finally:
        dispatcher.close()


async def test_lifecycle_archive_failure_retries_after_runtime_retirement() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    lifecycle_calls: list[WebhookLifecycleDelivery] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        claimed = await get_conversation_delivery(delivery.identity)
        assert claimed is not None
        assert claimed.status is ConversationDeliveryStatus.CLAIMED
        lifecycle_calls.append(delivery)

    route = _route(handle_lifecycle)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        first_event = _message_event("message-before-archive-failure")
        conversation_id = await _admit(dispatcher, route, first_event)
        await dispatcher.wake(conversation_id)
        binding = await get_conversation_control_binding(conversation_id)
        assert binding is not None

        original_set_thread_closed = harness.channel.set_thread_closed
        failed_once = False

        async def fail_archive_once(thread_jid: str, *, closed: bool) -> None:
            nonlocal failed_once
            if closed and not failed_once:
                failed_once = True
                raise RuntimeError("Discord archive failed")
            await original_set_thread_closed(thread_jid, closed=closed)

        harness.channel.set_thread_closed = fail_archive_once
        terminal_event = _lifecycle_event("archive-retry")
        await _admit(dispatcher, route, terminal_event)

        assert binding.thread_jid not in harness.workspace_map
        assert harness.retired_folders
        with pytest.raises(RuntimeError, match="Discord archive failed"):
            await dispatcher.wake(conversation_id)

        retryable = await get_conversation_delivery(
            _delivery_identity(route, terminal_event.delivery_id)
        )
        assert retryable is not None
        assert retryable.status is ConversationDeliveryStatus.PENDING
        assert len(lifecycle_calls) == 1

        await dispatcher.wake(conversation_id)

        completed = await get_conversation_delivery(
            _delivery_identity(route, terminal_event.delivery_id)
        )
        assert completed is not None
        assert completed.status is ConversationDeliveryStatus.COMPLETED
        assert len(lifecycle_calls) == 2
        assert harness.channel.closed[binding.thread_jid] is True
    finally:
        dispatcher.close()


async def test_recovery_retries_a_claimed_lifecycle_delivery_without_an_agent_turn() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    seen_lifecycles: list[WebhookLifecycleDelivery] = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        claimed = await get_conversation_delivery(delivery.identity)
        assert claimed is not None
        assert claimed.status is ConversationDeliveryStatus.CLAIMED
        seen_lifecycles.append(delivery)

    route = _route(handle_lifecycle)
    original = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await original.start()
    try:
        conversation_id = await _admit(
            dispatcher=original,
            route=route,
            event=_lifecycle_event("retry"),
        )
        claimed = await claim_next_conversation_delivery(
            conversation_id,
            ConversationClaimId("lifecycle-claim-before-restart"),
        )
        assert claimed is not None
        await prepare_conversation_delivery_recovery()
    finally:
        original.close()

    restarted = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await restarted.start()
    try:
        delivery = await get_conversation_delivery(claimed.identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert len(seen_lifecycles) == 1
        assert not harness.ingested
        assert not harness.channel.created
    finally:
        restarted.close()
