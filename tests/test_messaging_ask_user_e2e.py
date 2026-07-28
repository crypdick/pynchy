"""End-to-end integration tests for the ask_user blocking flow.

Exercises the full round-trip:
  container sends ask_user:ask IPC request → pending question file created
  → channel's send_ask_user called → user answers via callback
  → IPC response written (or cold-start enqueued) → pending file deleted

Three scenarios:
  1. Happy path — container alive, answer delivered via IPC response
  2. Late answer — container dead, answer triggers cold-start via message enqueue
  3. No channel support — channel lacks send_ask_user, error response written
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullIpcDeps, make_settings

from pynchy.host.container_manager.ipc import dispatch
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.orchestrator.messaging.ask_user_handler import (
    AskUserRuntimeOperations,
    handle_ask_user_answer,
)
from pynchy.host.orchestrator.messaging.pending_questions import (
    create_pending_question,
    resolve_pending_question,
    update_message_id,
)
from pynchy.state import init_test_database
from pynchy.types import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
async def _setup():
    await init_test_database()


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


TEST_GROUP = WorkspaceProfile(
    jid="chat@g.us",
    name="Test",
    folder="mygroup",
    trigger="@Bot",
    added_at="2024-01-01",
)

REQUEST_ID = "aabb001122334455"
QUESTIONS = [{"question": "Pick auth", "options": ["JWT", "OAuth"]}]


# ---------------------------------------------------------------------------
# Fake deps
# ---------------------------------------------------------------------------


class FakeChannel:
    """Channel that supports send_ask_user (Slack-like)."""

    name = "fake-slack"

    def __init__(self) -> None:
        self.send_ask_user = AsyncMock(return_value="msg-ts-123")

    def owns_jid(self, jid: str) -> bool:
        return jid == "chat@g.us"

    def is_connected(self) -> bool:
        return True


class BasicChannel:
    """Channel without send_ask_user (e.g. plain WhatsApp)."""

    name = "basic-channel"

    def owns_jid(self, jid: str) -> bool:
        return jid == "chat@g.us"

    def is_connected(self) -> bool:
        return True


class FakeIpcDeps(NullIpcDeps):
    """Minimal IpcDeps for ask_user handler tests."""

    def __init__(
        self,
        groups: dict[str, WorkspaceProfile],
        channels: list,
        active_sessions: dict[str, str] | None = None,
    ):
        self._groups = groups
        self._channels = channels
        self._active_sessions = active_sessions or {}

    def pending_question_store(self) -> _PendingQuestionStore:
        return _PendingQuestionStore()

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    def channels(self) -> list:
        return self._channels

    def get_active_sessions(self) -> dict[str, str]:
        return self._active_sessions


class _PendingQuestionStore:
    """Test adapter that preserves the real pending-question file behavior."""

    create = staticmethod(create_pending_question)
    update_message_id = staticmethod(update_message_id)
    resolve = staticmethod(resolve_pending_question)


class FakeAskUserDeps:
    """Minimal AskUserDeps for answer handler tests."""

    def __init__(self) -> None:
        self.enqueue_message = AsyncMock()
        self.has_live_session = MagicMock(return_value=False)
        self.ask_user_runtime_operations = AskUserRuntimeOperations(
            has_live_session=self.has_live_session,
            persist_skill_access=lambda _pending, _answer: None,
            write_response=self._write_response,
        )

    def has_active_host_process(self, _group_folder: str) -> bool:
        return False

    @staticmethod
    def _write_response(group_folder: str, request_id: str, result: dict[str, object]) -> None:
        write_ipc_response(ipc_response_path(group_folder, request_id), {"result": result})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.action("user.question.ask")
class TestAskUserE2E:
    """Full round-trip integration tests for ask_user."""

    @pytest.mark.asyncio
    async def test_happy_path_container_alive(self, tmp_path: Path, settings):
        """IPC request → pending question → channel send → answer → IPC response → cleanup."""
        channel = FakeChannel()
        deps = FakeIpcDeps(
            groups={"chat@g.us": TEST_GROUP},
            channels=[channel],
            active_sessions={"chat@g.us": "session-abc"},
        )

        # Step 1: Container sends ask_user:ask IPC request
        data = {
            "type": "ask_user:ask",
            "request_id": REQUEST_ID,
            "questions": QUESTIONS,
        }

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await dispatch(data, "mygroup", False, deps)

        # Verify: pending question file created
        pending_path = tmp_path / "ipc" / "mygroup" / "pending_questions" / f"{REQUEST_ID}.json"
        assert pending_path.exists(), "Pending question file should be created"
        pending_data = json.loads(pending_path.read_text())
        assert pending_data["request_id"] == REQUEST_ID
        assert pending_data["chat_jid"] == "chat@g.us"
        assert pending_data["questions"] == QUESTIONS
        assert pending_data["source_group"] == "mygroup"

        # Verify: channel.send_ask_user was called with correct args
        channel.send_ask_user.assert_awaited_once_with("chat@g.us", REQUEST_ID, QUESTIONS)

        # Verify: message_id updated in pending file
        updated_data = json.loads(pending_path.read_text())
        assert updated_data["message_id"] == "msg-ts-123"

        # Step 2: Simulate user answering via channel callback
        ask_user_deps = FakeAskUserDeps()

        ask_user_deps.has_live_session.return_value = True

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await handle_ask_user_answer(REQUEST_ID, {"answer": "JWT"}, ask_user_deps)

        # Verify: IPC response file written with the answer
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / f"{REQUEST_ID}.json"
        assert response_path.exists(), "Response file should be written for alive container"
        response_data = json.loads(response_path.read_text())
        assert response_data == {"result": {"answers": {"answer": "JWT"}}}

        # Verify: pending question file cleaned up
        assert not pending_path.exists(), "Pending question file should be deleted after answer"

        # Verify: enqueue_message was NOT called (container alive, no cold-start)
        ask_user_deps.enqueue_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_late_answer_cold_start(self, tmp_path: Path, settings):
        """Container dead when answer arrives — triggers cold-start via message enqueue."""
        # Step 1: Manually create a pending question file (simulates leftover from crash)
        pending_dir = tmp_path / "ipc" / "mygroup" / "pending_questions"
        pending_dir.mkdir(parents=True)

        request_id = "req-late-123"
        pending_data = {
            "request_id": request_id,
            "short_id": request_id[:8],
            "source_group": "mygroup",
            "chat_jid": "chat@g.us",
            "channel_name": "fake-slack",
            "session_id": "dead-session",
            "questions": QUESTIONS,
            "message_id": None,
            "timestamp": "2026-01-01T00:00:00+00:00",
        }
        (pending_dir / f"{request_id}.json").write_text(json.dumps(pending_data))

        # Step 2: Answer arrives with container dead
        ask_user_deps = FakeAskUserDeps()

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await handle_ask_user_answer(request_id, {"answer": "OAuth"}, ask_user_deps)

        # Verify: enqueue_message was called for cold-start
        ask_user_deps.enqueue_message.assert_awaited_once()
        call_jid, call_text = ask_user_deps.enqueue_message.call_args[0]
        assert call_jid == "chat@g.us"
        assert "Pick auth" in call_text, "Cold-start message should contain original question"
        assert "OAuth" in call_text, "Cold-start message should contain user's answer"

        # Verify: response file was NOT written (cold-start path doesn't write IPC response)
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / f"{request_id}.json"
        assert not response_path.exists(), "No IPC response for dead container"

        # Verify: pending question file cleaned up
        pending_path = pending_dir / f"{request_id}.json"
        assert not pending_path.exists(), "Pending question file should be deleted after answer"

    @pytest.mark.asyncio
    async def test_no_channel_writes_error(self, tmp_path: Path, settings):
        """Channel without send_ask_user → error response written immediately."""
        channel = BasicChannel()
        deps = FakeIpcDeps(
            groups={"chat@g.us": TEST_GROUP},
            channels=[channel],
        )

        data = {
            "type": "ask_user:ask",
            "request_id": REQUEST_ID,
            "questions": QUESTIONS,
        }

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir", settings.data_dir / "ipc"
            ),
        ):
            await dispatch(data, "mygroup", False, deps)

        # Verify: error response written
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / f"{REQUEST_ID}.json"
        assert response_path.exists(), "Error response should be written"
        response_data = json.loads(response_path.read_text())
        assert "error" in response_data
        assert "does not support" in response_data["error"]

        # Verify: pending question file is cleaned up immediately (no orphan).
        pending_path = tmp_path / "ipc" / "mygroup" / "pending_questions" / f"{REQUEST_ID}.json"
        assert not pending_path.exists(), "Pending question should be deleted after error response"
