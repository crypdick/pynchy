"""Business coverage for provider-neutral lifecycle-only webhook deliveries."""

from __future__ import annotations

# allow: file-length -- lifecycle delivery scenarios stay together for fixture reuse.
import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from linear_webhook_test_support import LinearWebhookHarness

from pynchy.conversation.models import (
    ConversationDelivery,
    ConversationDeliveryCompletion,
    ConversationDeliveryStatus,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalRoute,
    TerminalConversationRetirement,
)
from pynchy.conversation.workspaces import dynamic_thread_folder, routed_conversation_folder
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    EnsuredConversationWorkspace,
    ensure_conversation_workspace,
)
from pynchy.host.orchestrator.webhook_conversations import (
    ConversationWebhookDeps,
    WebhookConversationDispatcher,
)
from pynchy.host.orchestrator.webhook_delivery_admission import (
    WebhookDeliveryAdmissionRequest,
    admit_prepared_event,
)
from pynchy.host.orchestrator.webhook_terminal_retirement import retire_terminal_runtime
from pynchy.identifiers import SessionId
from pynchy.state import (
    WebhookReceipt,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_delivery,
    get_webhook_receipt,
    set_conversation_session,
)
from tests.webhook_lifecycle_support import (
    _admit,
    _conversation,
    _delivery_identity,
    _lifecycle_event,
    _message_event,
    _receipt,
    _route,
)

pytest_plugins = ("tests.webhook_lifecycle_support",)

if TYPE_CHECKING:
    from pynchy.plugins.api import (
        NewMessage,
        WebhookEvent,
        WebhookLifecycleDelivery,
    )

_NOW = datetime(2026, 7, 26, tzinfo=UTC).isoformat()
_SUBJECT = ConversationSubject(
    namespace=ConversationSubjectNamespace("test-provider:tenant:issue"),
    key=ConversationSubjectKey("issue-1"),
)


async def test_routed_admission_requires_a_live_dispatcher() -> None:
    route = _route()
    event = _message_event("missing-dispatcher")

    with pytest.raises(RuntimeError, match="dispatcher disappeared"):
        await admit_prepared_event(
            None,
            route,
            event,
            WebhookDeliveryAdmissionRequest(
                receipt=_receipt(route, event),
                task=None,
                defer_process_event=False,
            ),
        )


async def test_project_open_control_requires_a_workspace_owner() -> None:
    route = replace(_route(), workspace=None, candidate_workspaces=("project",))
    event = replace(
        _message_event("missing-workspace"),
        conversation=replace(_conversation(closed=False), workspace=None),
    )
    dispatcher = WebhookConversationDispatcher(
        deps=MagicMock(spec=ConversationWebhookDeps), routes=(route,)
    )

    with pytest.raises(RuntimeError, match="has no workspace owner"):
        await dispatcher.project_open_control(route, event)


async def test_project_open_control_ignores_an_unchanged_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.apply_conversation_control_state",
        AsyncMock(return_value=False),
    )

    assert await dispatcher.project_open_control(route, _message_event("unchanged")) is None


async def test_wake_completes_a_deferred_delivery_without_reopening_closed_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    delivery = ConversationDelivery(
        sequence=1,
        identity=_delivery_identity(route, "deferred-closed"),
        conversation_id=ConversationId("conversation-1"),
        status=ConversationDeliveryStatus.CLAIMED,
        received_at=_NOW,
        payload={"control_closed": True},
    )
    completion = ConversationDeliveryCompletion(
        identity=delivery.identity,
        conversation_id=delivery.conversation_id,
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.claim_next_conversation_delivery",
        AsyncMock(side_effect=[delivery, delivery]),
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.process_deferred_event",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.complete_webhook_delivery",
        AsyncMock(side_effect=[completion, None]),
    )
    notify = AsyncMock()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.notify_conversation_delivery_completed",
        notify,
    )

    await dispatcher.wake(delivery.conversation_id)
    await dispatcher.wake(delivery.conversation_id)

    notify.assert_awaited_once_with(completion)
    foreign = ConversationDeliveryCompletion(
        identity=replace(delivery.identity, route=ExternalRoute("other")),
        conversation_id=delivery.conversation_id,
    )
    await dispatcher.after_completion(foreign)
    assert notify.await_count == 1


async def test_admission_fails_when_parsed_route_target_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    route = _route()
    event = _message_event("missing-target")
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.conversation_admission_request",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="lost its parsed route target"):
        await dispatcher.admit_webhook(
            route,
            event,
            "Routed webhook prompt",
            _receipt(route, event),
            defer_process_event=False,
        )


async def test_dispatcher_preparation_defers_pending_delivery_domain_work() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()

    route = _route(AsyncMock())
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    dispatcher.prepare()
    try:
        await _admit(dispatcher, route, _message_event("pending-at-startup"))

        assert harness.ingested == []
        assert harness.channel.created == []

        await dispatcher.recover_pending()

        assert len(harness.ingested) == 1
    finally:
        dispatcher.close()


async def test_terminal_admission_rolls_back_when_retirement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    route = _route()
    event = _lifecycle_event("terminal-rollback")
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))

    async def fail_retirement(  # noqa: RUF029 - injected state callback is async.
        *_args: object,
        **_kwargs: object,
    ) -> None:
        raise RuntimeError("retirement failed")

    monkeypatch.setattr(
        "pynchy.state.webhooks._retire_conversation_for_terminal",
        fail_retirement,
    )

    with pytest.raises(RuntimeError, match="retirement failed"):
        await dispatcher.admit_webhook(
            route,
            event,
            None,
            _receipt(route, event),
            defer_process_event=False,
        )

    assert await get_webhook_receipt(route.provider, route.name, event.delivery_id) is None
    assert await get_conversation_delivery(_delivery_identity(route, event.delivery_id)) is None


async def test_ignored_open_reopen_restores_existing_thread_runtime() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        conversation_id = await _admit(
            dispatcher,
            route,
            _message_event("ignored-open-base", revision="2026-07-27T00:00:00+00:00"),
        )
        await dispatcher.wake(conversation_id)
        binding = await get_conversation_control_binding(conversation_id)
        assert binding is not None

        await _admit(
            dispatcher,
            route,
            _lifecycle_event("ignored-open-terminal", revision="2026-07-27T00:00:01+00:00"),
        )
        await dispatcher.wake(conversation_id)
        assert binding.thread_jid not in harness.workspace_map
        assert harness.channel.closed[binding.thread_jid] is True

        event = replace(
            _message_event("ignored-open-reopen", revision="2026-07-27T00:00:02+00:00"),
            instructions=None,
            external_context=None,
            ignored_reason="controller_owned_open_state",
        )
        receipt = WebhookReceipt(
            provider=route.provider,
            route=route.name,
            delivery_id=event.delivery_id,
            workspace="project",
            event_type=event.event_type,
            event_action=event.action,
            subject_id=event.subject_id,
            payload_sha256="sha-ignored-open-reopen",
            disposition="ignored",
            ignored_reason=event.ignored_reason,
            task_id=None,
            occurred_at=event.occurred_at,
            received_at=_NOW,
        )
        admission, reopened_id = await admit_prepared_event(
            dispatcher,
            route,
            event,
            WebhookDeliveryAdmissionRequest(
                receipt=receipt,
                task=None,
                defer_process_event=False,
            ),
        )

        assert admission.created is True
        assert reopened_id is None
        assert binding.thread_jid in harness.workspace_map
        assert harness.channel.closed[binding.thread_jid] is False
    finally:
        dispatcher.close()


async def test_ignored_open_without_reopened_conversation_skips_runtime_restore() -> None:
    route = _route()
    event = _message_event("ignored-open-no-reopen")
    receipt = replace(
        _receipt(route, event),
        disposition="ignored",
        ignored_reason="duplicate_delivery",
    )
    dispatcher = WebhookConversationDispatcher(
        deps=MagicMock(spec=ConversationWebhookDeps), routes=(route,)
    )
    dispatcher.deps.channels.return_value = []
    project_open_control = AsyncMock(return_value=None)
    restore_runtime = AsyncMock()
    with (
        patch.object(WebhookConversationDispatcher, "project_open_control", project_open_control),
        patch.object(
            WebhookConversationDispatcher,
            "restore_existing_open_control_runtime",
            restore_runtime,
        ),
    ):
        admission, conversation_id = await admit_prepared_event(
            dispatcher,
            route,
            event,
            WebhookDeliveryAdmissionRequest(
                receipt=receipt,
                task=None,
                defer_process_event=False,
            ),
        )

    assert admission.created is True
    assert conversation_id is None
    restore_runtime.assert_not_awaited()


async def test_deferred_controller_event_reopens_control_without_agent_turn() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()

    async def controller_owned(event: WebhookEvent) -> WebhookEvent:
        await asyncio.sleep(0)
        return replace(
            event,
            instructions=None,
            external_context=None,
            ignored_reason="work_item_execution_owned_by_controller",
        )

    route = replace(_route(), process_event=controller_owned)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    dispatcher.prepare()
    try:
        conversation_id = await _admit(
            dispatcher,
            route,
            _message_event("controller-open-base", revision="2026-07-27T00:00:00+00:00"),
        )
        await dispatcher.wake(conversation_id)
        binding = await get_conversation_control_binding(conversation_id)
        assert binding is not None

        await _admit(
            dispatcher,
            route,
            _lifecycle_event("controller-open-terminal", revision="2026-07-27T00:00:01+00:00"),
        )
        await dispatcher.wake(conversation_id)
        assert harness.channel.closed[binding.thread_jid] is True
        assert binding.thread_jid not in harness.workspace_map

        event = _message_event(
            "controller-open-reopen",
            revision="2026-07-27T00:00:02+00:00",
        )
        receipt = WebhookReceipt(
            provider=route.provider,
            route=route.name,
            delivery_id=event.delivery_id,
            workspace="project",
            event_type=event.event_type,
            event_action=event.action,
            subject_id=event.subject_id,
            payload_sha256="sha-controller-open-reopen",
            disposition="routed",
            ignored_reason=None,
            task_id=None,
            occurred_at=event.occurred_at,
            received_at=_NOW,
        )
        admission, reopened_id = await admit_prepared_event(
            dispatcher,
            route,
            event,
            WebhookDeliveryAdmissionRequest(
                receipt=receipt,
                task=None,
                defer_process_event=True,
            ),
        )
        assert admission.created is True
        assert reopened_id == conversation_id

        await dispatcher.wake(conversation_id)

        reopened = await get_conversation_control_binding(conversation_id)
        assert reopened is not None
        assert reopened.closed is False
        assert harness.channel.closed[binding.thread_jid] is False
        assert binding.thread_jid in harness.workspace_map
        assert [message.id for message in harness.ingested] == ["controller-open-base"]
    finally:
        dispatcher.close()


async def test_lifecycle_retires_older_turn_immediately_and_suppresses_its_sibling() -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    seen_lifecycles: list[WebhookLifecycleDelivery] = []
    binding_at_callback = []

    async def handle_lifecycle(delivery: WebhookLifecycleDelivery) -> None:
        binding = await get_conversation_control_binding(delivery.conversation_id)
        binding_at_callback.append(binding)
        seen_lifecycles.append(delivery)

    route = _route(handle_lifecycle)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    await dispatcher.start()
    try:
        first_event = _message_event("message-before-terminal")
        with patch(
            "pynchy.state.conversation_routing.secrets.token_urlsafe",
            return_value="id-ending-in-hyphen-",
        ):
            conversation_id = await _admit(dispatcher, route, first_event)
        await dispatcher.wake(conversation_id)
        before_close = await get_conversation_control_binding(conversation_id)
        assert before_close is not None
        await set_conversation_session(conversation_id, SessionId("terminal-session"))

        terminal_event = _lifecycle_event("terminal-state")
        await _admit(dispatcher, route, terminal_event)
        first_delivery = await get_conversation_delivery(
            _delivery_identity(route, first_event.delivery_id)
        )
        terminal_delivery = await get_conversation_delivery(
            _delivery_identity(route, terminal_event.delivery_id)
        )
        retired = await get_conversation(conversation_id)
        assert first_delivery is not None
        assert first_delivery.status is ConversationDeliveryStatus.COMPLETED
        assert terminal_delivery is not None
        assert terminal_delivery.status is ConversationDeliveryStatus.PENDING
        assert retired is not None
        assert retired.control_closed is True
        assert retired.session_id is None
        assert harness.retired_task_conversations == [str(conversation_id)]
        assert set(harness.retired_folders) == {
            routed_conversation_folder("project", conversation_id),
            dynamic_thread_folder("project", before_close.thread_jid),
        }
        assert before_close.thread_jid not in harness.workspace_map

        await dispatcher.wake(conversation_id)
        after_event = _message_event("message-after-terminal", closed=None)
        await _admit(dispatcher, route, after_event)
        await dispatcher.wake(conversation_id)

        assert len(harness.ingested) == 1
        assert [message.id for message in harness.ingested] == ["message-before-terminal"]
        terminal_delivery = await get_conversation_delivery(
            _delivery_identity(route, terminal_event.delivery_id)
        )
        sibling_delivery = await get_conversation_delivery(
            _delivery_identity(route, after_event.delivery_id)
        )
        after_close = await get_conversation_control_binding(conversation_id)
        assert terminal_delivery is not None
        assert terminal_delivery.status is ConversationDeliveryStatus.COMPLETED
        assert sibling_delivery is not None
        assert sibling_delivery.status is ConversationDeliveryStatus.COMPLETED
        assert len(seen_lifecycles) == 1
        assert seen_lifecycles[0].subject_id == "issue-1"
        assert seen_lifecycles[0].context == {"state_id": "done-state"}
        assert len(binding_at_callback) == 1
        assert binding_at_callback[0] is not None
        assert binding_at_callback[0].closed is True
        assert after_close is not None
        assert after_close.closed is True
        assert after_close.parent_workspace == before_close.parent_workspace
        assert after_close.parent_jid == before_close.parent_jid
        assert after_close.thread_jid == before_close.thread_jid
        assert after_close.title == before_close.title
        assert harness.channel.closed[before_close.thread_jid] is True

        replay, replay_conversation_id = await dispatcher.admit_webhook(
            route,
            terminal_event,
            None,
            _receipt(route, terminal_event),
            defer_process_event=False,
        )
        assert replay.created is False
        assert replay_conversation_id == conversation_id
        await dispatcher.wake(conversation_id)
        assert len(seen_lifecycles) == 1
    finally:
        dispatcher.close()


async def test_newer_terminal_suppresses_a_claimed_superseded_lifecycle_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    callbacks: list[str] = []

    async def handle_lifecycle(  # noqa: RUF029 - WebhookRoute lifecycle contract is async.
        delivery: WebhookLifecycleDelivery,
    ) -> None:
        callbacks.append(str(delivery.identity.delivery_id))

    route = _route(handle_lifecycle)
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    dispatcher.prepare()
    entered = asyncio.Event()
    release = asyncio.Event()
    newer_terminal_retired = asyncio.Event()

    async def record_newer_terminal_retirement(
        deps: LinearWebhookHarness,
        conversation_id: ConversationId,
        retirement: TerminalConversationRetirement,
        runtime_workspace_folders: set[str],
    ) -> bool:
        if retirement.control_state_revision == "2026-07-27T00:00:01+00:00":
            newer_terminal_retired.set()
        return await retire_terminal_runtime(
            deps,
            conversation_id,
            retirement,
            runtime_workspace_folders,
        )

    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.retire_terminal_runtime",
        record_newer_terminal_retirement,
    )

    async def hold_after_first_lifecycle_fence(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_delivery_processing.sync_conversation_control_state",
        hold_after_first_lifecycle_fence,
    )
    try:
        conversation_id = await _admit(
            dispatcher,
            route,
            _lifecycle_event("terminal-old", revision="2026-07-27T00:00:00+00:00"),
        )
        old_wake = asyncio.create_task(dispatcher.wake(conversation_id))
        await asyncio.wait_for(entered.wait(), timeout=1)

        newer_terminal = asyncio.create_task(
            _admit(
                dispatcher,
                route,
                _lifecycle_event("terminal-new", revision="2026-07-27T00:00:01+00:00"),
            )
        )
        await asyncio.wait_for(newer_terminal_retired.wait(), timeout=1)
        release.set()
        await old_wake
        await newer_terminal

        old_delivery = await get_conversation_delivery(_delivery_identity(route, "terminal-old"))
        assert old_delivery is not None
        assert old_delivery.status is ConversationDeliveryStatus.COMPLETED
        assert callbacks == []

        await dispatcher.wake(conversation_id)
        assert callbacks == ["terminal-new"]
    finally:
        dispatcher.close()


async def test_terminal_retirement_suppresses_claimed_stale_nonterminal_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    dispatcher.prepare()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_before_control_projection(
        delivery: object,
        claim_id: object,
        deferred_route: object,
    ) -> object:
        del claim_id, deferred_route
        entered.set()
        await release.wait()
        return delivery

    monkeypatch.setattr(
        "pynchy.host.orchestrator.webhook_conversations.process_deferred_event",
        hold_before_control_projection,
    )
    try:
        conversation_id = await _admit(
            dispatcher,
            route,
            _message_event("nonterminal-old", revision="2026-07-27T00:00:00+00:00"),
        )
        stale_wake = asyncio.create_task(dispatcher.wake(conversation_id))
        await asyncio.wait_for(entered.wait(), timeout=1)

        await _admit(
            dispatcher,
            route,
            _lifecycle_event("terminal-new", revision="2026-07-27T00:00:01+00:00"),
        )
        release.set()
        await stale_wake

        conversation = await get_conversation(conversation_id)
        stale_delivery = await get_conversation_delivery(
            _delivery_identity(route, "nonterminal-old")
        )
        assert conversation is not None
        assert conversation.control_closed is True
        assert conversation.control_state_revision == "2026-07-27T00:00:01+00:00"
        assert stale_delivery is not None
        assert stale_delivery.status is ConversationDeliveryStatus.COMPLETED
        assert harness.ingested == []
    finally:
        dispatcher.close()


async def test_terminal_cleanup_serializes_new_nonterminal_runtime_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = LinearWebhookHarness()
    await harness.persist_parent()
    route = _route()
    dispatcher = WebhookConversationDispatcher(deps=harness, routes=(route,))
    dispatcher.prepare()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    projection_started = asyncio.Event()
    reopened_ingested = asyncio.Event()
    original_runtime_retirement = harness.retire_conversation_runtime

    async def hold_runtime_retirement(folder: str) -> None:
        cleanup_entered.set()
        await release_cleanup.wait()
        await original_runtime_retirement(folder)
        cleanup_finished.set()

    monkeypatch.setattr(harness, "retire_conversation_runtime", hold_runtime_retirement)
    try:
        conversation_id = await _admit(
            dispatcher,
            route,
            _message_event("projection-before-terminal", revision="2026-07-27T00:00:00+00:00"),
        )
        await dispatcher.wake(conversation_id)

        original_ensure = ensure_conversation_workspace

        async def assert_cleanup_precedes_projection(
            context: ConversationWorkspaceContext,
            request: ConversationControlRequest,
        ) -> EnsuredConversationWorkspace:
            assert cleanup_finished.is_set()
            projection_started.set()
            return await original_ensure(context, request)

        monkeypatch.setattr(
            "pynchy.host.orchestrator.webhook_delivery_processing.ensure_conversation_workspace",
            assert_cleanup_precedes_projection,
        )
        original_ingest = harness.ingest_message

        async def record_reopened_ingest(jid: str, message: NewMessage) -> None:
            await original_ingest(jid, message)
            if message.id == "projection-reopen":
                reopened_ingested.set()

        monkeypatch.setattr(harness, "ingest_message", record_reopened_ingest)

        terminal = asyncio.create_task(
            _admit(
                dispatcher,
                route,
                _lifecycle_event("projection-terminal", revision="2026-07-27T00:00:01+00:00"),
            )
        )
        await asyncio.wait_for(cleanup_entered.wait(), timeout=1)

        reopened_id = await _admit(
            dispatcher,
            route,
            _message_event("projection-reopen", revision="2026-07-27T00:00:02+00:00"),
        )
        reopened = await get_conversation(reopened_id)
        assert reopened is not None
        assert reopened.control_closed is False
        assert reopened.control_state_revision == "2026-07-27T00:00:02+00:00"

        wake = asyncio.create_task(dispatcher.wake(reopened_id))
        await asyncio.sleep(0)
        assert not projection_started.is_set()

        release_cleanup.set()
        await terminal
        await wake
        await asyncio.wait_for(projection_started.wait(), timeout=1)
        await asyncio.wait_for(reopened_ingested.wait(), timeout=1)

        assert [message.id for message in harness.ingested] == [
            "projection-before-terminal",
            "projection-reopen",
        ]
    finally:
        release_cleanup.set()
        dispatcher.close()
