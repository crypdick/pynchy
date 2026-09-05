"""Tests for composed plugin-owned session lifecycle work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pluggy
import pytest

from pynchy.host.orchestrator import session_handler
from pynchy.identifiers import RuntimeId
from pynchy.plugins.api import NewMessage, PynchySpec, prepare_context_reset
from pynchy.plugins.integrations.linear import LinearMcpPlugin
from pynchy.plugins.integrations.linear_session_reset import LinearSessionResetState
from pynchy.workspace.api import WorkspaceProfile

hookimpl = pluggy.HookimplMarker("pynchy")


class _LifecyclePlugin:
    def __init__(self, before_reset: AsyncMock) -> None:
        self.before_reset = before_reset

    @hookimpl
    async def pynchy_before_context_reset(self, group: WorkspaceProfile) -> None:
        await self.before_reset(group)


@dataclass(frozen=True)
class _HookRoot:
    pynchy_before_context_reset: object


@dataclass(frozen=True)
class _MalformedPluginManager:
    hook: _HookRoot


async def test_context_reset_awaits_each_valid_participant() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    first = AsyncMock()
    second = AsyncMock()
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_LifecyclePlugin(first), name="first")
    manager.register(_LifecyclePlugin(second), name="second")

    await prepare_context_reset(manager, group)

    first.assert_awaited_once_with(group)
    second.assert_awaited_once_with(group)


async def test_context_reset_settles_every_participant_before_propagating_failure() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    completed = AsyncMock()
    failing = AsyncMock(side_effect=RuntimeError("settlement failed"))
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_LifecyclePlugin(completed), name="completed")
    manager.register(_LifecyclePlugin(failing), name="failing")

    with pytest.raises(RuntimeError, match="settlement failed"):
        await prepare_context_reset(manager, group)

    completed.assert_awaited_once_with(group)
    failing.assert_awaited_once_with(group)


async def test_context_reset_allows_an_empty_plugin_registry() -> None:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)

    await prepare_context_reset(
        manager,
        WorkspaceProfile(jid="slack:C123", name="Test", folder="test", trigger="@pynchy"),
    )


async def test_context_reset_reports_all_plugin_failures_together() -> None:
    class _FailingPlugin:
        def __init__(self, error: BaseException) -> None:
            self._error = error

        @hookimpl
        async def pynchy_before_context_reset(self, group: WorkspaceProfile) -> None:
            del group
            raise self._error

    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_FailingPlugin(RuntimeError("first")))
    manager.register(_FailingPlugin(ValueError("second")))

    with pytest.raises(BaseExceptionGroup) as raised:
        await prepare_context_reset(
            manager,
            WorkspaceProfile(jid="slack:C123", name="Test", folder="test", trigger="@pynchy"),
        )

    assert {type(error) for error in raised.value.exceptions} == {RuntimeError, ValueError}


async def test_linear_plugin_settles_its_execution_before_reset() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    cancel_workflow = AsyncMock(return_value=True)
    with patch(
        "pynchy.plugins.integrations.linear.cancel_linear_execution_for_reset",
        new_callable=AsyncMock,
    ) as cancel:
        state = LinearSessionResetState(
            get_control_by_thread=MagicMock(),
            get_conversation=MagicMock(),
            get_active_execution=MagicMock(),
            cancel_task=MagicMock(),
            cancel_execution=MagicMock(),
        )
        await LinearMcpPlugin(
            cancel_scheduled_workflow=cancel_workflow,
            session_reset_state=state,
        ).pynchy_before_context_reset(group)

    cancel.assert_awaited_once_with(
        group,
        cancel_scheduled_workflow=cancel_workflow,
        state=state,
    )


async def test_invalid_lifecycle_hook_fails_closed() -> None:
    class _InvalidPlugin:
        @hookimpl
        def pynchy_before_context_reset(self, group: WorkspaceProfile) -> None:
            del group

    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_InvalidPlugin())

    with pytest.raises(TypeError, match="must return an awaitable"):
        await prepare_context_reset(
            manager,
            WorkspaceProfile(
                jid="slack:C123",
                name="Test",
                folder="test",
                trigger="@pynchy",
            ),
        )


async def test_missing_plugin_manager_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="Plugin manager is unavailable"):
        await prepare_context_reset(
            None,
            WorkspaceProfile(
                jid="slack:C123",
                name="Test",
                folder="test",
                trigger="@pynchy",
            ),
        )


async def test_context_reset_rejects_a_hook_result_count_mismatch() -> None:
    hook = MagicMock()
    hook.get_hookimpls.return_value = [object(), object()]
    hook.return_value = [asyncio.sleep(0)]
    manager = _MalformedPluginManager(_HookRoot(hook))

    with pytest.raises(TypeError, match="must return an awaitable"):
        await prepare_context_reset(
            manager,
            WorkspaceProfile(jid="slack:C123", name="Test", folder="test", trigger="@pynchy"),
        )


async def test_context_reset_ignores_non_coroutine_in_count_mismatch() -> None:
    hook = MagicMock()
    hook.get_hookimpls.return_value = [object(), object()]
    hook.return_value = [object()]
    manager = _MalformedPluginManager(_HookRoot(hook))

    with pytest.raises(TypeError, match="must return an awaitable"):
        await prepare_context_reset(
            manager,
            WorkspaceProfile(jid="slack:C123", name="Test", folder="test", trigger="@pynchy"),
        )


async def test_context_reset_closes_pending_hooks_after_a_non_awaitable() -> None:
    hook = MagicMock()
    hook.get_hookimpls.return_value = [object(), object()]
    pending = asyncio.sleep(0)
    hook.return_value = [object(), pending]
    manager = _MalformedPluginManager(_HookRoot(hook))

    with pytest.raises(TypeError, match="must return an awaitable"):
        await prepare_context_reset(
            manager,
            WorkspaceProfile(jid="slack:C123", name="Test", folder="test", trigger="@pynchy"),
        )


async def test_scheduled_reset_settles_plugins_before_destructive_cleanup() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    deps = MagicMock(spec=session_handler.ResetSessionDeps)
    deps.prepare_context_reset = AsyncMock(side_effect=RuntimeError("not settled"))
    deps.destroy_runtime_session = AsyncMock()

    with (
        patch(
            "pynchy.host.orchestrator.session_handler.clear_session",
            new_callable=AsyncMock,
        ) as clear_session,
        pytest.raises(RuntimeError, match="not settled"),
    ):
        await session_handler.handle_scheduled_context_reset(
            deps,
            "task-1",
            group,
            "occurrence-1",
        )

    deps.prepare_context_reset.assert_awaited_once_with(group)
    deps.destroy_runtime_session.assert_not_awaited()
    clear_session.assert_not_awaited()


async def test_clear_confirmation_persists_boundary_and_notifies_completed_deliveries() -> None:
    deps = MagicMock(spec=session_handler.SessionDeps)
    completion = object()

    with (
        patch(
            "pynchy.host.orchestrator.session_handler.set_chat_cleared_at",
            new_callable=AsyncMock,
            return_value=[completion],
        ) as set_cleared,
        patch(
            "pynchy.host.orchestrator.session_handler.notify_conversation_delivery_completed",
            new_callable=AsyncMock,
        ) as notify,
        patch(
            "pynchy.host.orchestrator.session_handler._send_command_confirmation",
            new_callable=AsyncMock,
        ) as confirm,
    ):
        await session_handler.send_clear_confirmation(deps, "slack:C123")

    set_cleared.assert_awaited_once()
    deps.emit.assert_called_once()
    confirm.assert_awaited_once_with(deps, "slack:C123", None, "🗑️")
    notify.assert_awaited_once_with(completion)


async def test_manual_reset_stops_worker_before_plugin_settlement() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    events: list[str] = []
    deps = MagicMock(spec=session_handler.ResetSessionDeps)
    deps.queue.stop_active_process_for_control = AsyncMock(
        side_effect=lambda _runtime_id: events.append("stopped")
    )
    deps.destroy_runtime_session = AsyncMock(
        side_effect=lambda _runtime_id: events.append("destroyed")
    )
    deps.prepare_context_reset = AsyncMock(side_effect=lambda _group: events.append("settled"))
    deps.sessions = {}
    deps.session_cleared = set()
    deps.queue.clear_pending_tasks = MagicMock()
    deps.save_state = AsyncMock()
    deps.channels = []
    deps.emit = MagicMock()

    with (
        patch(
            "pynchy.host.orchestrator.session_handler.clear_session",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.orchestrator.session_handler.advance_cursor",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.orchestrator.session_handler.send_clear_confirmation",
            new_callable=AsyncMock,
        ),
    ):
        await session_handler.handle_context_reset(
            deps,
            group.jid,
            group,
            "2026-07-25T00:00:00Z",
        )

    assert events == ["stopped", "settled", "destroyed"]


async def test_end_session_stops_runtime_and_reacts_on_owned_channel() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    deps = MagicMock(spec=session_handler.SessionDeps)
    deps.queue.destroy_runtime_session = AsyncMock()
    deps.queue.stop_active_process = AsyncMock()
    deps.queue.clear_pending_tasks = MagicMock()

    class _OwnedChannel:
        def owns_jid(self, jid: str) -> bool:
            return jid == group.jid

    deps.channels = [_OwnedChannel()]
    source_message = NewMessage(
        id="message-1",
        chat_jid=group.jid,
        sender="sender-1",
        sender_name="Sender",
        content="end session",
        timestamp="2026-07-29T00:00:00Z",
    )
    background_names: list[str | None] = []

    def discard_background(coro, *, name=None):
        coro.close()
        background_names.append(name)

    with (
        patch(
            "pynchy.host.orchestrator.session_handler.create_background_task",
            side_effect=discard_background,
        ),
        patch(
            "pynchy.host.orchestrator.session_handler.advance_cursor",
            new_callable=AsyncMock,
        ) as advance,
        patch(
            "pynchy.host.orchestrator.session_handler.send_reaction_to_channels",
            new_callable=AsyncMock,
        ) as react,
    ):
        await session_handler.handle_end_session(
            deps,
            group.jid,
            group,
            source_message.timestamp,
            source_message=source_message,
        )

    assert background_names == ["destroy-session-test", "stop-container-slack:C123"]
    deps.queue.clear_pending_tasks.assert_called_once_with(RuntimeId(group.folder))
    advance.assert_awaited_once_with(deps, group.jid, source_message.timestamp)
    react.assert_awaited_once_with(
        deps,
        group.jid,
        source_message.id,
        source_message.sender,
        "👋",
    )


async def test_manual_redeploy_without_source_message_broadcasts_confirmation() -> None:
    deps = MagicMock(spec=session_handler.SessionDeps)
    deps.current_deploy_revision.return_value = ("sha", "config")
    deps.broadcast_host_message = AsyncMock()

    with patch(
        "pynchy.host.orchestrator.session_handler.start_deploy_workflow",
        new_callable=AsyncMock,
    ):
        await session_handler.trigger_manual_redeploy(deps, "chat")

    deps.broadcast_host_message.assert_awaited_once_with("chat", "🔄")


async def test_inbound_without_owned_channel_skips_read_receipt() -> None:
    class _Channel:
        def __init__(self, name: str, owned: bool) -> None:
            self.name = name
            self._owned = owned

        def owns_jid(self, _jid: str) -> bool:
            return self._owned

    deps = MagicMock(spec=session_handler.SessionDeps)
    deps.channels = [_Channel("other", False), _Channel("owner", True)]
    deps.workspaces = {}
    message = NewMessage(
        id="message-1",
        chat_jid="chat",
        sender="sender",
        sender_name="Sender",
        content="hello",
        timestamp="2026-07-29T00:00:00Z",
    )

    with (
        patch(
            "pynchy.host.orchestrator.session_handler._ensure_dynamic_thread_workspace",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.orchestrator.session_handler.ingest_user_message",
            new_callable=AsyncMock,
        ) as ingest,
    ):
        await session_handler.on_inbound(deps, "chat", message)

    ingest.assert_awaited_once_with(deps, message, source_channel="owner")


async def test_inbound_without_any_channel_finishes_channel_scan() -> None:
    deps = MagicMock(spec=session_handler.SessionDeps)
    deps.channels = []
    deps.workspaces = {}
    message = NewMessage(
        id="message-2",
        chat_jid="chat",
        sender="sender",
        sender_name="Sender",
        content="hello",
        timestamp="2026-07-29T00:00:00Z",
    )

    with (
        patch(
            "pynchy.host.orchestrator.session_handler._ensure_dynamic_thread_workspace",
            new_callable=AsyncMock,
        ),
        patch(
            "pynchy.host.orchestrator.session_handler.ingest_user_message",
            new_callable=AsyncMock,
        ) as ingest,
    ):
        await session_handler.on_inbound(deps, "chat", message)

    ingest.assert_awaited_once_with(deps, message, source_channel=None)
