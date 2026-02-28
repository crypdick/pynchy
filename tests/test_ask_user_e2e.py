"""End-to-end integration tests for the ask_user blocking flow.

Exercises the full round-trip:
  container sends ask_user:ask IPC task → pending question file created
  → channel's send_ask_user called → user answers via callback
  → IPC response written (or cold-start enqueued) → pending file deleted

Three scenarios:
  1. Happy path — container alive, answer delivered via IPC response
  2. Late answer — container dead, answer triggers cold-start via message enqueue
  3. No channel support — channel lacks send_ask_user, error response written
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.state import _init_test_database
from pynchy.types import WorkspaceProfile


@pytest.fixture(autouse=True)
async def _setup():
    await _init_test_database()


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


class FakeIpcDeps:
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

    def workspaces(self) -> dict[str, WorkspaceProfile]:
        return self._groups

    def channels(self) -> list:
        return self._channels

    def get_active_sessions(self) -> dict[str, str]:
        return self._active_sessions


class FakeAskUserDeps:
    """Minimal AskUserDeps for answer handler tests."""

    def __init__(self) -> None:
        self.enqueue_message = AsyncMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


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

        # Step 1: Container sends ask_user:ask IPC task
        from pynchy.host.container_manager.ipc.handlers_ask_user import _handle_ask_user_request

        data = {
            "type": "ask_user:ask",
            "request_id": REQUEST_ID,
            "questions": QUESTIONS,
        }

        with (
            patch("pynchy.host.orchestrator.messaging.pending_questions.get_settings", return_value=settings),
            patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        ):
            await _handle_ask_user_request(data, "mygroup", False, deps)

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
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        ask_user_deps = FakeAskUserDeps()

        # Mock get_session to return an alive session
        fake_session = type("FakeSession", (), {"is_alive": True})()

        with (
            patch("pynchy.host.orchestrator.messaging.pending_questions.get_settings", return_value=settings),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.get_session", return_value=fake_session),
            patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
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
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        ask_user_deps = FakeAskUserDeps()

        with (
            patch("pynchy.host.orchestrator.messaging.pending_questions.get_settings", return_value=settings),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.get_session", return_value=None),
            patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
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

        from pynchy.host.container_manager.ipc.handlers_ask_user import _handle_ask_user_request

        data = {
            "type": "ask_user:ask",
            "request_id": REQUEST_ID,
            "questions": QUESTIONS,
        }

        with (
            patch("pynchy.host.orchestrator.messaging.pending_questions.get_settings", return_value=settings),
            patch("pynchy.host.container_manager.ipc.write.get_settings", return_value=settings),
        ):
            await _handle_ask_user_request(data, "mygroup", False, deps)

        # Verify: error response written
        response_path = tmp_path / "ipc" / "mygroup" / "responses" / f"{REQUEST_ID}.json"
        assert response_path.exists(), "Error response should be written"
        response_data = json.loads(response_path.read_text())
        assert "error" in response_data
        assert "does not support" in response_data["error"]

        # Verify: pending question file is cleaned up immediately (no orphan).
        pending_path = tmp_path / "ipc" / "mygroup" / "pending_questions" / f"{REQUEST_ID}.json"
        assert not pending_path.exists(), "Pending question should be deleted after error response"
