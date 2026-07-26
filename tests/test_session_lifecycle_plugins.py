"""Tests for composed plugin-owned session lifecycle work."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pluggy
import pytest

from pynchy.host.orchestrator import session_handler
from pynchy.plugins.hookspecs import PynchySpec
from pynchy.plugins.integrations.linear import LinearMcpPlugin
from pynchy.plugins.session_lifecycle import prepare_context_reset
from pynchy.types import WorkspaceProfile

hookimpl = pluggy.HookimplMarker("pynchy")


class _LifecyclePlugin:
    def __init__(self, before_reset: AsyncMock) -> None:
        self.before_reset = before_reset

    @hookimpl
    async def pynchy_before_context_reset(self, group: WorkspaceProfile) -> None:
        await self.before_reset(group)


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


async def test_linear_plugin_settles_its_execution_before_reset() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    with patch(
        "pynchy.plugins.integrations.linear.cancel_linear_execution_for_reset",
        new_callable=AsyncMock,
    ) as cancel:
        await LinearMcpPlugin().pynchy_before_context_reset(group)

    cancel.assert_awaited_once_with(group)


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


async def test_scheduled_reset_settles_plugins_before_destructive_cleanup() -> None:
    group = WorkspaceProfile(
        jid="slack:C123",
        name="Test",
        folder="test",
        trigger="@pynchy",
    )
    deps = MagicMock(spec=session_handler.ResetSessionDeps)
    deps.prepare_context_reset = AsyncMock(side_effect=RuntimeError("not settled"))

    with (
        patch(
            "pynchy.host.orchestrator.session_handler.destroy_session",
            new_callable=AsyncMock,
        ) as destroy_session,
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
    destroy_session.assert_not_awaited()
    clear_session.assert_not_awaited()


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
    deps.prepare_context_reset = AsyncMock(side_effect=lambda _group: events.append("settled"))
    deps.sessions = {}
    deps.session_cleared = set()
    deps.queue.clear_pending_tasks = MagicMock()
    deps.save_state = AsyncMock()
    deps.channels = []
    deps.emit = MagicMock()

    with (
        patch(
            "pynchy.host.orchestrator.session_handler.destroy_session",
            new_callable=AsyncMock,
            side_effect=lambda _folder: events.append("destroyed"),
        ),
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
