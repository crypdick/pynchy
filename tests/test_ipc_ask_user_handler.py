"""Tests for the IPC ask_user handler."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.host.container_manager.ipc.deps import AskUserDeps, IpcDeps
from pynchy.host.container_manager.ipc.registry import PREFIX_HANDLERS, dispatch
from pynchy.plugins.api import Channel
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


def _make_deps(
    *,
    workspaces: dict[str, WorkspaceProfile] | None = None,
    channels: list | None = None,
    active_sessions: dict[str, str] | None = None,
) -> MagicMock:
    """Build a mock IpcDeps with the fields the ask_user handler needs."""
    deps = MagicMock(spec=AskUserDeps)
    deps.workspaces.return_value = workspaces or {}
    deps.channels.return_value = channels or []
    deps.get_active_sessions.return_value = active_sessions or {}
    deps.pending_question_store.return_value = MagicMock()
    return deps


def _make_workspace(
    jid: str = "group@g.us",
    folder: str = "my-group",
    name: str = "My Group",
) -> WorkspaceProfile:
    return WorkspaceProfile(jid=jid, name=name, folder=folder, trigger="@Bot")


def _make_channel(
    name: str = "slack",
    owns_jid: bool = True,
    has_send_ask_user: bool = True,
    send_ask_user_return: str | None = "msg-42",
) -> MagicMock:
    """Build a mock channel."""
    ch = MagicMock(spec=Channel)
    ch.name = name
    ch.owns_jid.return_value = owns_jid
    if has_send_ask_user:
        ch.send_ask_user = AsyncMock(return_value=send_ask_user_return)
    else:
        # Remove the attribute so hasattr returns False
        del ch.send_ask_user
    return ch


class TestHandleAskUserRequest:
    @pytest.mark.asyncio
    async def test_requires_pending_question_dependencies(self):
        with pytest.raises(TypeError, match="pending-question persistence"):
            await dispatch(
                {"type": "ask_user:ask", "request_id": "req123hex"},
                "my-group",
                False,
                MagicMock(spec=IpcDeps),
            )

    @pytest.mark.asyncio
    async def test_stores_pending_question_with_correct_fields(self):
        """Handler should call create_pending_question with the right arguments."""
        ws = _make_workspace(jid="group@g.us", folder="my-group")
        channel = _make_channel(name="slack")
        deps = _make_deps(
            workspaces={"group@g.us": ws},
            channels=[channel],
            active_sessions={"group@g.us": "session-abc"},
        )

        data = {
            "type": "ask_user:ask",
            "request_id": "req123hex",
            "questions": [{"question": "Which auth?", "options": ["OAuth", "API key"]}],
        }

        await dispatch(data, "my-group", False, deps)

        deps.pending_question_store.return_value.create.assert_called_once_with(
            request_id="req123hex",
            source_group="my-group",
            chat_jid="group@g.us",
            channel_name="slack",
            session_id="session-abc",
            questions=[{"question": "Which auth?", "options": ["OAuth", "API key"]}],
        )

    @pytest.mark.asyncio
    async def test_calls_send_ask_user_on_channel(self):
        """Handler should call channel.send_ask_user with the right arguments."""
        ws = _make_workspace(jid="group@g.us", folder="my-group")
        channel = _make_channel(name="slack", send_ask_user_return="msg-42")
        deps = _make_deps(
            workspaces={"group@g.us": ws},
            channels=[channel],
            active_sessions={"group@g.us": "session-abc"},
        )

        questions = [{"question": "Which auth?", "options": ["OAuth", "API key"]}]
        data = {
            "type": "ask_user:ask",
            "request_id": "req123hex",
            "questions": questions,
        }

        await dispatch(data, "my-group", False, deps)

        channel.send_ask_user.assert_awaited_once_with("group@g.us", "req123hex", questions)

    @pytest.mark.asyncio
    async def test_updates_message_id_when_channel_returns_one(self):
        """Handler should call update_message_id when send_ask_user returns a value."""
        ws = _make_workspace(jid="group@g.us", folder="my-group")
        channel = _make_channel(name="slack", send_ask_user_return="msg-42")
        deps = _make_deps(
            workspaces={"group@g.us": ws},
            channels=[channel],
            active_sessions={"group@g.us": "session-abc"},
        )

        data = {
            "type": "ask_user:ask",
            "request_id": "req123hex",
            "questions": [{"question": "Which auth?"}],
        }

        await dispatch(data, "my-group", False, deps)

        deps.pending_question_store.return_value.update_message_id.assert_called_once_with(
            "req123hex", "my-group", "msg-42"
        )

    @pytest.mark.asyncio
    async def test_skips_message_id_update_when_channel_returns_none(self):
        """Handler should NOT call update_message_id when send_ask_user returns None."""
        ws = _make_workspace(jid="group@g.us", folder="my-group")
        channel = _make_channel(name="slack", send_ask_user_return=None)
        deps = _make_deps(
            workspaces={"group@g.us": ws},
            channels=[channel],
            active_sessions={"group@g.us": "session-abc"},
        )

        data = {
            "type": "ask_user:ask",
            "request_id": "req123hex",
            "questions": [{"question": "Which auth?"}],
        }

        await dispatch(data, "my-group", False, deps)

        deps.pending_question_store.return_value.update_message_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_error_response_when_channel_lacks_send_ask_user(self, settings):
        """Handler should write an IPC error if the channel has no send_ask_user."""
        ws = _make_workspace(jid="group@g.us", folder="my-group")
        channel = _make_channel(name="whatsapp", has_send_ask_user=False)
        deps = _make_deps(
            workspaces={"group@g.us": ws},
            channels=[channel],
            active_sessions={"group@g.us": "session-abc"},
        )

        data = {
            "type": "ask_user:ask",
            "request_id": "req123hex",
            "questions": [{"question": "Which auth?"}],
        }

        with (
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await dispatch(data, "my-group", False, deps)

        # An error response file should have been written
        response_file = settings.data_dir / "ipc" / "my-group" / "responses" / "req123hex.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "whatsapp" in response["error"].lower()

        # The pending question should be cleaned up immediately
        deps.pending_question_store.return_value.resolve.assert_called_once_with(
            "req123hex", "my-group"
        )

    @pytest.mark.asyncio
    async def test_handles_missing_request_id_gracefully(self):
        """Handler should return early without crashing when request_id is missing."""
        deps = _make_deps()

        data = {
            "type": "ask_user:ask",
            "questions": [{"question": "Which auth?"}],
            # No request_id
        }

        # Should not raise
        await dispatch(data, "my-group", False, deps)

        # create_pending_question should NOT have been called
        deps.pending_question_store.return_value.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_writes_error_when_no_workspace_matches_group(self, settings):
        """Handler should write an IPC error if no workspace maps to source_group."""
        # Workspace folder doesn't match source_group
        ws = _make_workspace(jid="other@g.us", folder="other-group")
        deps = _make_deps(workspaces={"other@g.us": ws})

        data = {
            "type": "ask_user:ask",
            "request_id": "req999",
            "questions": [{"question": "Which auth?"}],
        }

        with (
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await dispatch(data, "my-group", False, deps)

        deps.pending_question_store.return_value.create.assert_not_called()

        response_file = settings.data_dir / "ipc" / "my-group" / "responses" / "req999.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert "error" in response

    @pytest.mark.asyncio
    async def test_writes_error_when_no_channel_owns_jid(self, settings):
        """Handler should write an IPC error if no channel owns the group's JID."""
        ws = _make_workspace(jid="group@g.us", folder="my-group")
        # Channel does NOT own this JID
        channel = _make_channel(name="slack", owns_jid=False)
        deps = _make_deps(
            workspaces={"group@g.us": ws},
            channels=[channel],
        )

        data = {
            "type": "ask_user:ask",
            "request_id": "req888",
            "questions": [{"question": "Which auth?"}],
        }

        with (
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await dispatch(data, "my-group", False, deps)

        deps.pending_question_store.return_value.create.assert_not_called()

        response_file = settings.data_dir / "ipc" / "my-group" / "responses" / "req888.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert "error" in response

    def test_handler_registered_with_ask_user_prefix(self):
        """The 'ask_user:' prefix should be routable through the dispatcher."""
        assert "ask_user:" in PREFIX_HANDLERS
