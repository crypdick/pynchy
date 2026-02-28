"""Tests for reaction_handler — emoji reaction routing to actions."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.orchestrator.messaging.reaction_handler import _REACTION_ACTIONS, handle_reaction
from pynchy.types import WorkspaceProfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_JID = "group@g.us"
TEST_GROUP = WorkspaceProfile(
    jid="group@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


class FakeReactionDeps:
    """Minimal ReactionDeps implementation for testing."""

    def __init__(
        self,
        groups: dict[str, WorkspaceProfile] | None = None,
        is_active: bool = False,
    ) -> None:
        self.workspaces: dict[str, WorkspaceProfile] = groups or {}
        self.queue = MagicMock()
        self.queue.is_active_task.return_value = is_active
        self.queue.stop_active_process = AsyncMock()
        self.broadcast_to_channels = AsyncMock()

    async def broadcast_to_channels(
        self, chat_jid: str, text: str, *, suppress_errors: bool = True
    ) -> None:
        pass  # pragma: no cover — overridden by AsyncMock


# ---------------------------------------------------------------------------
# Reaction mapping tests
# ---------------------------------------------------------------------------


class TestReactionActionMapping:
    """Verify the emoji → action mapping is correct."""

    def test_eyes_maps_to_retry(self):
        assert _REACTION_ACTIONS["eyes"] == "retry"

    def test_x_maps_to_interrupt(self):
        assert _REACTION_ACTIONS["x"] == "interrupt"

    def test_unknown_emoji_not_in_mapping(self):
        assert "thumbsup" not in _REACTION_ACTIONS
        assert "heart" not in _REACTION_ACTIONS


# ---------------------------------------------------------------------------
# handle_reaction tests
# ---------------------------------------------------------------------------


class TestHandleReaction:
    """Tests for the handle_reaction routing logic."""

    @pytest.mark.asyncio
    async def test_unknown_emoji_is_ignored(self):
        """Reactions with unmapped emoji should be no-ops."""
        deps = FakeReactionDeps(groups={TEST_JID: TEST_GROUP})
        await handle_reaction(deps, TEST_JID, "msg-ts", "user-1", "thumbsup")
        deps.queue.enqueue_message_check.assert_not_called()
        deps.queue.stop_active_process.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregistered_group_is_ignored(self):
        """Reactions from unregistered groups should be no-ops."""
        deps = FakeReactionDeps(groups={})  # no registered groups
        await handle_reaction(deps, TEST_JID, "msg-ts", "user-1", "eyes")
        deps.queue.enqueue_message_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_eyes_triggers_retry(self):
        """Eyes emoji should enqueue a message check (retry)."""
        deps = FakeReactionDeps(groups={TEST_JID: TEST_GROUP})
        await handle_reaction(deps, TEST_JID, "msg-ts", "user-1", "eyes")
        deps.queue.enqueue_message_check.assert_called_once_with(TEST_JID)

    @pytest.mark.asyncio
    async def test_x_interrupt_when_active(self):
        """X emoji with active task should stop the process and broadcast."""
        deps = FakeReactionDeps(groups={TEST_JID: TEST_GROUP}, is_active=True)
        with patch("pynchy.utils.create_background_task") as mock_bg:
            await handle_reaction(deps, TEST_JID, "msg-ts", "user-1", "x")

        deps.queue.is_active_task.assert_called_once_with(TEST_JID)
        deps.queue.clear_pending_tasks.assert_called_once_with(TEST_JID)
        # stop_active_process should be scheduled as a background task
        mock_bg.assert_called_once()
        deps.broadcast_to_channels.assert_called_once_with(TEST_JID, "Interrupted by reaction.")
        mock_bg.call_args[0][0].close()  # discard unawaited coroutine from AsyncMock

    @pytest.mark.asyncio
    async def test_x_no_active_task_is_noop(self):
        """X emoji with no active task should not interrupt anything."""
        deps = FakeReactionDeps(groups={TEST_JID: TEST_GROUP}, is_active=False)
        await handle_reaction(deps, TEST_JID, "msg-ts", "user-1", "x")
        deps.queue.is_active_task.assert_called_once_with(TEST_JID)
        deps.queue.clear_pending_tasks.assert_not_called()
        deps.broadcast_to_channels.assert_not_called()

    @pytest.mark.asyncio
    async def test_different_jid_for_group_lookup(self):
        """Reaction routing uses the jid parameter for group lookup."""
        other_jid = "other@g.us"
        deps = FakeReactionDeps(groups={TEST_JID: TEST_GROUP})
        # other_jid is not registered, so this should be a no-op
        await handle_reaction(deps, other_jid, "msg-ts", "user-1", "eyes")
        deps.queue.enqueue_message_check.assert_not_called()
