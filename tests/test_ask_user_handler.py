"""Tests for the ask_user answer delivery handler.

Covers:
- Path A: container alive -> writes IPC response
- Path B: container dead -> injects synthetic message for cold-start
- Unknown question -> logs warning and returns
- Answer context formatting
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


@pytest.fixture
def pending_question():
    """A typical pending question dict as returned by find_pending_question."""
    return {
        "request_id": "req-abc123",
        "short_id": "req-abc1",
        "source_group": "test-group",
        "chat_jid": "slack:C123",
        "channel_name": "slack",
        "session_id": "sess-456",
        "questions": [
            {
                "question": "Which auth strategy?",
                "options": ["JWT tokens", "Session cookies", "OAuth 2.0"],
            }
        ],
        "message_id": "1234567890.123456",
        "timestamp": "2026-02-24T12:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Path A: container alive -> write IPC response
# ---------------------------------------------------------------------------


class TestPathAContainerAlive:
    @pytest.mark.asyncio
    async def test_writes_ipc_response_when_alive(self, settings, pending_question):
        """When the container is alive, write the answer as an IPC response file."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        alive_session = MagicMock()
        alive_session.is_alive = True

        deps = MagicMock()
        deps.enqueue_message = AsyncMock()
        answer = {"auth_strategy": "JWT tokens"}

        with (
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.find_pending_question",
                return_value=pending_question,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.get_session",
                return_value=alive_session,
            ),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.write_ipc_response") as mock_write,
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.ipc_response_path",
                return_value=Path("/tmp/fake/responses/req-abc123.json"),
            ) as mock_path,
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.resolve_pending_question"),
        ):
            await handle_ask_user_answer("req-abc123", answer, deps)

        mock_path.assert_called_once_with("test-group", "req-abc123")
        mock_write.assert_called_once_with(
            Path("/tmp/fake/responses/req-abc123.json"),
            {"result": {"answers": {"auth_strategy": "JWT tokens"}}},
        )
        # Should NOT enqueue a message (container is alive)
        deps.enqueue_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolves_pending_question_when_alive(self, settings, pending_question):
        """After writing IPC response, the pending question file should be resolved."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        alive_session = MagicMock()
        alive_session.is_alive = True
        deps = MagicMock()
        deps.enqueue_message = AsyncMock()

        with (
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.find_pending_question",
                return_value=pending_question,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.get_session",
                return_value=alive_session,
            ),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.write_ipc_response"),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.ipc_response_path", return_value=Path("/tmp/x")),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.resolve_pending_question") as mock_resolve,
        ):
            await handle_ask_user_answer("req-abc123", {"choice": "A"}, deps)

        mock_resolve.assert_called_once_with("req-abc123", "test-group")


# ---------------------------------------------------------------------------
# Path B: container dead -> cold-start with answer context
# ---------------------------------------------------------------------------


class TestPathBContainerDead:
    @pytest.mark.asyncio
    async def test_enqueues_message_when_dead(self, settings, pending_question):
        """When the container is dead, enqueue the answer as a synthetic message."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        deps = MagicMock()
        deps.enqueue_message = AsyncMock()

        with (
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.find_pending_question",
                return_value=pending_question,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.get_session",
                return_value=None,  # container dead
            ),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.resolve_pending_question"),
        ):
            await handle_ask_user_answer("req-abc123", {"auth_strategy": "JWT tokens"}, deps)

        deps.enqueue_message.assert_called_once()
        call_args = deps.enqueue_message.call_args
        chat_jid = call_args[0][0]
        text = call_args[0][1]

        assert chat_jid == "slack:C123"
        assert "Which auth strategy?" in text
        assert "JWT tokens" in text

    @pytest.mark.asyncio
    async def test_enqueues_message_when_session_not_alive(self, settings, pending_question):
        """A session that exists but is_alive=False should trigger cold-start path."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        dead_session = MagicMock()
        dead_session.is_alive = False
        deps = MagicMock()
        deps.enqueue_message = AsyncMock()

        with (
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.find_pending_question",
                return_value=pending_question,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.get_session",
                return_value=dead_session,
            ),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.resolve_pending_question"),
        ):
            await handle_ask_user_answer("req-abc123", {"choice": "X"}, deps)

        deps.enqueue_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolves_pending_question_when_dead(self, settings, pending_question):
        """The pending question should be resolved even in the cold-start path."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        deps = MagicMock()
        deps.enqueue_message = AsyncMock()

        with (
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.find_pending_question",
                return_value=pending_question,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.get_session",
                return_value=None,
            ),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.resolve_pending_question") as mock_resolve,
        ):
            await handle_ask_user_answer("req-abc123", {"a": "b"}, deps)

        mock_resolve.assert_called_once_with("req-abc123", "test-group")


# ---------------------------------------------------------------------------
# Unknown question -> warning + early return
# ---------------------------------------------------------------------------


class TestUnknownQuestion:
    @pytest.mark.asyncio
    async def test_unknown_question_returns_early(self, settings):
        """If the question doesn't exist, log a warning and don't crash."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import handle_ask_user_answer

        deps = MagicMock()
        deps.enqueue_message = AsyncMock()

        with (
            patch(
                "pynchy.host.orchestrator.messaging.ask_user_handler.find_pending_question",
                return_value=None,
            ),
            patch("pynchy.host.orchestrator.messaging.ask_user_handler.resolve_pending_question") as mock_resolve,
        ):
            # Should not raise
            await handle_ask_user_answer("nonexistent-id", {"x": "y"}, deps)

        # Should not try to resolve a question that doesn't exist
        mock_resolve.assert_not_called()
        deps.enqueue_message.assert_not_called()


# ---------------------------------------------------------------------------
# _format_answer_context
# ---------------------------------------------------------------------------


class TestFormatAnswerContext:
    def test_format_with_options(self, pending_question):
        """Format context text with question and options."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import _format_answer_context

        text = _format_answer_context(pending_question, {"auth_strategy": "JWT tokens"})

        assert "Which auth strategy?" in text
        assert "1. JWT tokens" in text
        assert "2. Session cookies" in text
        assert "3. OAuth 2.0" in text
        assert "Continue from where you left off" in text

    def test_format_with_dict_options(self):
        """Dict options with label/description should render labels, not raw dicts."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import _format_answer_context

        pending = {
            "questions": [
                {
                    "question": "Which auth strategy?",
                    "options": [
                        {"label": "JWT", "description": "Stateless auth"},
                        {"label": "OAuth", "description": "Delegated auth"},
                    ],
                }
            ],
        }
        text = _format_answer_context(pending, {"answer": "JWT"})

        assert "1. JWT" in text
        assert "2. OAuth" in text
        # Should NOT contain raw dict repr
        assert "{'label'" not in text
        assert '{"label"' not in text

    def test_format_without_options(self):
        """Format context text when the question has no options."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import _format_answer_context

        pending = {
            "questions": [{"question": "What should the timeout be?"}],
        }
        text = _format_answer_context(pending, {"timeout": "30s"})

        assert "What should the timeout be?" in text
        assert "30s" in text

    def test_format_multiple_questions(self):
        """Format context text with multiple questions."""
        from pynchy.host.orchestrator.messaging.ask_user_handler import _format_answer_context

        pending = {
            "questions": [
                {"question": "Pick a color", "options": ["red", "blue"]},
                {"question": "Pick a size", "options": ["S", "M", "L"]},
            ],
        }
        text = _format_answer_context(pending, {"color": "red", "size": "M"})

        assert "Pick a color" in text
        assert "Pick a size" in text
        assert "red" in text
        assert "M" in text
