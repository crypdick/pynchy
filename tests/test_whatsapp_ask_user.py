"""Tests for WhatsApp ask_user: numbered text fallback and answer interception."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.chat.pending_questions import find_pending_for_jid

# ---------------------------------------------------------------------------
# Neonize mock setup — must happen before importing WhatsAppChannel
# ---------------------------------------------------------------------------

# Create fake neonize modules so the WhatsApp channel module can be imported
# in environments where neonize (a native Go binding) isn't installed.
_NEONIZE_MODULES = [
    "neonize",
    "neonize.aioze",
    "neonize.aioze.client",
    "neonize.aioze.events",
    "neonize.events",
    "neonize.proto",
    "neonize.proto.Neonize_pb2",
    "neonize.utils",
    "neonize.utils.jid",
    "neonize.utils.enum",
]
_neonize_mocks: dict[str, ModuleType] = {}
for _mod_name in _NEONIZE_MODULES:
    if _mod_name not in sys.modules:
        _neonize_mocks[_mod_name] = MagicMock()
        sys.modules[_mod_name] = _neonize_mocks[_mod_name]

# Now it's safe to import
from pynchy.chat.plugins.whatsapp.channel import WhatsAppChannel  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_JID = "120363001234567890@g.us"
REQUEST_ID = "req-wa-test-001"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _questions_with_options() -> list[dict]:
    return [
        {
            "question": "Which auth strategy?",
            "options": [
                {"label": "JWT tokens", "description": "Stateless auth"},
                {"label": "Session cookies", "description": "Server-side sessions"},
                {"label": "OAuth 2.0", "description": "Delegated auth"},
            ],
        }
    ]


def _questions_with_string_options() -> list[dict]:
    """Options as plain strings instead of dicts."""
    return [
        {
            "question": "Pick a color",
            "options": ["Red", "Green", "Blue"],
        }
    ]


def _questions_no_options() -> list[dict]:
    return [
        {
            "question": "What is the project name?",
        }
    ]


def _pending_data(
    *,
    chat_jid: str = CHAT_JID,
    request_id: str = REQUEST_ID,
    questions: list[dict] | None = None,
    timestamp: str | None = None,
) -> dict:
    from datetime import UTC, datetime

    return {
        "request_id": request_id,
        "short_id": request_id[:8],
        "source_group": "test-group",
        "chat_jid": chat_jid,
        "channel_name": "connection.whatsapp.main",
        "session_id": "sess-001",
        "questions": questions or _questions_with_options(),
        "message_id": None,
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }


def _make_channel(
    *,
    on_ask_user_answer: object | None = None,
) -> WhatsAppChannel:
    """Create a WhatsAppChannel with mocked internals.

    Uses __new__ to bypass __init__ which requires neonize native bindings.
    """
    ch = WhatsAppChannel.__new__(WhatsAppChannel)
    # Manually initialise the attributes we care about
    ch.name = "connection.whatsapp.test"
    ch._connection_name = "connection.whatsapp.test"
    ch._on_message = MagicMock()
    ch._on_chat_metadata = MagicMock()
    ch._on_ask_user_answer = on_ask_user_answer
    ch._workspaces = lambda: {CHAT_JID: MagicMock()}
    ch._connected = True
    ch._outgoing_queue = MagicMock()
    ch._lid_to_phone = {}
    ch.send_message = AsyncMock()
    return ch


# ---------------------------------------------------------------------------
# send_ask_user tests
# ---------------------------------------------------------------------------


class TestSendAskUser:
    @pytest.mark.asyncio
    async def test_formats_numbered_text(self) -> None:
        """Verify numbered text formatting with options."""
        ch = _make_channel()
        await ch.send_ask_user(CHAT_JID, REQUEST_ID, _questions_with_options())

        ch.send_message.assert_called_once()
        text = ch.send_message.call_args[0][1]

        assert "Which auth strategy?" in text
        assert "1. JWT tokens" in text
        assert "2. Session cookies" in text
        assert "3. OAuth 2.0" in text
        assert "Reply with a number" in text

    @pytest.mark.asyncio
    async def test_formats_string_options(self) -> None:
        """Options as plain strings should also render correctly."""
        ch = _make_channel()
        await ch.send_ask_user(CHAT_JID, REQUEST_ID, _questions_with_string_options())

        text = ch.send_message.call_args[0][1]
        assert "1. Red" in text
        assert "2. Green" in text
        assert "3. Blue" in text

    @pytest.mark.asyncio
    async def test_no_options(self) -> None:
        """Question with no options should still format correctly."""
        ch = _make_channel()
        await ch.send_ask_user(CHAT_JID, REQUEST_ID, _questions_no_options())

        text = ch.send_message.call_args[0][1]
        assert "What is the project name?" in text
        assert "Reply with your answer" in text
        # Should NOT contain numbered options
        assert "1." not in text

    @pytest.mark.asyncio
    async def test_returns_message_id(self) -> None:
        """send_ask_user returns a string message ID."""
        ch = _make_channel()
        result = await ch.send_ask_user(CHAT_JID, REQUEST_ID, _questions_with_options())
        assert result is not None
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_sends_to_correct_jid(self) -> None:
        """The message is sent to the correct chat JID."""
        ch = _make_channel()
        await ch.send_ask_user(CHAT_JID, REQUEST_ID, _questions_with_options())

        sent_jid = ch.send_message.call_args[0][0]
        assert sent_jid == CHAT_JID


# ---------------------------------------------------------------------------
# _resolve_answer tests
# ---------------------------------------------------------------------------


class TestResolveAnswer:
    def test_number_match_dict_options(self) -> None:
        """Numeric reply matching a dict option returns the label."""
        ch = _make_channel()
        pending = _pending_data()
        answer = ch._resolve_answer("2", pending)
        assert answer == {"answer": "Session cookies"}

    def test_number_match_string_options(self) -> None:
        """Numeric reply matching a plain-string option returns the string."""
        ch = _make_channel()
        pending = _pending_data(questions=_questions_with_string_options())
        answer = ch._resolve_answer("1", pending)
        assert answer == {"answer": "Red"}

    def test_number_out_of_range(self) -> None:
        """Number beyond the option range is treated as free-form text."""
        ch = _make_channel()
        pending = _pending_data()
        answer = ch._resolve_answer("99", pending)
        assert answer == {"answer": "99"}

    def test_number_zero(self) -> None:
        """Zero is out of range (1-indexed) and treated as free-form."""
        ch = _make_channel()
        pending = _pending_data()
        answer = ch._resolve_answer("0", pending)
        assert answer == {"answer": "0"}

    def test_free_text(self) -> None:
        """Non-numeric text is returned as free-form answer."""
        ch = _make_channel()
        pending = _pending_data()
        answer = ch._resolve_answer("I want something else", pending)
        assert answer == {"answer": "I want something else"}

    def test_strips_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped before matching."""
        ch = _make_channel()
        pending = _pending_data()
        answer = ch._resolve_answer("  1  ", pending)
        assert answer == {"answer": "JWT tokens"}

    def test_no_questions_in_pending(self) -> None:
        """If pending has no questions, treat as free-form."""
        ch = _make_channel()
        pending = _pending_data(questions=[])
        answer = ch._resolve_answer("hello", pending)
        assert answer == {"answer": "hello"}

    def test_unicode_superscript_digit_treated_as_freeform(self) -> None:
        """Unicode superscript digits (e.g. '²') must NOT match as numeric option.

        str.isdigit() returns True for these but int() raises ValueError.
        The regex [0-9]+ guard ensures only ASCII digits are matched.
        """
        ch = _make_channel()
        pending = _pending_data()
        # '\u00b2' is superscript 2 — isdigit() returns True but int() would crash
        answer = ch._resolve_answer("\u00b2", pending)
        assert answer == {"answer": "\u00b2"}


# ---------------------------------------------------------------------------
# Answer interception tests
# ---------------------------------------------------------------------------


class TestAnswerIntercept:
    def test_number_match_calls_callback(self) -> None:
        """Incoming '2' while pending question exists triggers callback with matched label."""
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)
        pending = _pending_data()

        answer = ch._resolve_answer("2", pending)
        ch._on_ask_user_answer(pending["request_id"], answer)

        callback.assert_called_once_with(REQUEST_ID, {"answer": "Session cookies"})

    def test_free_text_calls_callback(self) -> None:
        """Non-numeric text triggers callback with raw text."""
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)
        pending = _pending_data()

        answer = ch._resolve_answer("Actually, use API keys", pending)
        ch._on_ask_user_answer(pending["request_id"], answer)

        callback.assert_called_once_with(REQUEST_ID, {"answer": "Actually, use API keys"})

    def test_skips_normal_pipeline(self) -> None:
        """When a pending question is intercepted, _on_message is NOT called."""
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)
        pending = _pending_data()

        # Simulate the interception path: resolve + call callback
        answer = ch._resolve_answer("1", pending)
        ch._on_ask_user_answer(pending["request_id"], answer)

        # _on_message should NOT have been called
        ch._on_message.assert_not_called()

    def test_no_callback_no_error(self) -> None:
        """If on_ask_user_answer is None, resolving an answer should not raise."""
        ch = _make_channel(on_ask_user_answer=None)
        pending = _pending_data()

        # This should work without error even with no callback
        answer = ch._resolve_answer("1", pending)
        assert answer == {"answer": "JWT tokens"}
        # When callback is None, the _handle_message code simply skips the call


# ---------------------------------------------------------------------------
# find_pending_for_jid tests
# ---------------------------------------------------------------------------


class TestFindPendingForJid:
    def test_finds_matching_jid(self, tmp_path: Path) -> None:
        """Finds a pending question by chat_jid across groups."""
        ipc_dir = tmp_path / "ipc"
        pq_dir = ipc_dir / "my-group" / "pending_questions"
        pq_dir.mkdir(parents=True)
        data = _pending_data()
        (pq_dir / f"{REQUEST_ID}.json").write_text(json.dumps(data))

        with patch("pynchy.chat.pending_questions.get_settings") as mock_settings:
            mock_settings.return_value.data_dir = tmp_path
            result = find_pending_for_jid(CHAT_JID)

        assert result is not None
        assert result["request_id"] == REQUEST_ID
        assert result["chat_jid"] == CHAT_JID

    def test_returns_none_when_no_match(self, tmp_path: Path) -> None:
        """Returns None when no pending question matches the JID."""
        ipc_dir = tmp_path / "ipc"
        pq_dir = ipc_dir / "my-group" / "pending_questions"
        pq_dir.mkdir(parents=True)
        data = _pending_data(chat_jid="different@g.us")
        (pq_dir / "other-req.json").write_text(json.dumps(data))

        with patch("pynchy.chat.pending_questions.get_settings") as mock_settings:
            mock_settings.return_value.data_dir = tmp_path
            result = find_pending_for_jid(CHAT_JID)

        assert result is None

    def test_returns_none_when_ipc_dir_missing(self, tmp_path: Path) -> None:
        """Returns None when ipc directory doesn't exist."""
        with patch("pynchy.chat.pending_questions.get_settings") as mock_settings:
            mock_settings.return_value.data_dir = tmp_path
            result = find_pending_for_jid(CHAT_JID)

        assert result is None

    def test_skips_errors_dir(self, tmp_path: Path) -> None:
        """The 'errors' directory is skipped during search."""
        ipc_dir = tmp_path / "ipc"
        errors_dir = ipc_dir / "errors" / "pending_questions"
        errors_dir.mkdir(parents=True)
        data = _pending_data()
        (errors_dir / f"{REQUEST_ID}.json").write_text(json.dumps(data))

        with patch("pynchy.chat.pending_questions.get_settings") as mock_settings:
            mock_settings.return_value.data_dir = tmp_path
            result = find_pending_for_jid(CHAT_JID)

        assert result is None

    def test_handles_corrupt_json(self, tmp_path: Path) -> None:
        """Corrupt JSON files are silently skipped."""
        ipc_dir = tmp_path / "ipc"
        pq_dir = ipc_dir / "my-group" / "pending_questions"
        pq_dir.mkdir(parents=True)
        (pq_dir / "corrupt.json").write_text("{bad json")

        # Also add a valid one that should still be found
        data = _pending_data()
        (pq_dir / f"{REQUEST_ID}.json").write_text(json.dumps(data))

        with patch("pynchy.chat.pending_questions.get_settings") as mock_settings:
            mock_settings.return_value.data_dir = tmp_path
            result = find_pending_for_jid(CHAT_JID)

        assert result is not None
        assert result["request_id"] == REQUEST_ID

    def test_searches_multiple_groups(self, tmp_path: Path) -> None:
        """Searches across multiple group directories."""
        ipc_dir = tmp_path / "ipc"

        # Group A — different JID
        pq_dir_a = ipc_dir / "group-a" / "pending_questions"
        pq_dir_a.mkdir(parents=True)
        data_a = _pending_data(chat_jid="other@g.us", request_id="req-a")
        (pq_dir_a / "req-a.json").write_text(json.dumps(data_a))

        # Group B — matching JID
        pq_dir_b = ipc_dir / "group-b" / "pending_questions"
        pq_dir_b.mkdir(parents=True)
        data_b = _pending_data(chat_jid=CHAT_JID, request_id="req-b")
        (pq_dir_b / "req-b.json").write_text(json.dumps(data_b))

        with patch("pynchy.chat.pending_questions.get_settings") as mock_settings:
            mock_settings.return_value.data_dir = tmp_path
            result = find_pending_for_jid(CHAT_JID)

        assert result is not None
        assert result["request_id"] == "req-b"


# ---------------------------------------------------------------------------
# Callback wiring tests
# ---------------------------------------------------------------------------


class TestCallbackWiring:
    def test_callback_stored_on_init(self) -> None:
        """on_ask_user_answer is stored as an instance attribute."""
        cb = MagicMock()
        ch = _make_channel(on_ask_user_answer=cb)
        assert ch._on_ask_user_answer is cb

    def test_callback_defaults_to_none(self) -> None:
        """on_ask_user_answer defaults to None."""
        ch = _make_channel(on_ask_user_answer=None)
        assert ch._on_ask_user_answer is None


# ---------------------------------------------------------------------------
# _handle_message integration tests
# ---------------------------------------------------------------------------


class TestHandleMessageIntercept:
    """End-to-end test that calls _handle_message and verifies the ask_user
    interception path triggers the callback and skips normal routing."""

    def _build_message_ev(self, content: str = "2") -> MagicMock:
        """Build a mock MessageEv with the structure _handle_message expects."""
        message = MagicMock()
        message.Info.MessageSource.Chat = MagicMock()  # JID object
        message.Info.MessageSource.IsFromMe = False
        message.Info.MessageSource.Sender = MagicMock()
        message.Info.MessageSource.Sender.User = "5551234"
        message.Info.Timestamp = 1740000000
        message.Info.ID = "msg-123"
        message.Info.Pushname = "Test User"
        message.Message.conversation = content
        message.Message.extendedTextMessage.text = ""
        message.Message.imageMessage.caption = ""
        message.Message.videoMessage.caption = ""
        return message

    @pytest.mark.asyncio
    async def test_intercepts_answer_and_skips_on_message(self) -> None:
        """A numeric reply to a pending question calls the callback and
        does NOT reach _on_message."""
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)

        message = self._build_message_ev("2")
        pending = _pending_data()

        # Patch Jid2String to return the CHAT_JID for any call
        jid2string_mock = sys.modules["neonize.utils.jid"].Jid2String
        jid2string_mock.return_value = CHAT_JID

        # Patch _translate_jid to return CHAT_JID (bypass LID translation)
        ch._translate_jid = MagicMock(return_value=CHAT_JID)

        with patch(
            "pynchy.chat.plugins.whatsapp.channel.find_pending_for_jid",
            return_value=pending,
        ):
            await ch._handle_message(message)

        # The ask_user callback should have been called with the resolved answer
        callback.assert_called_once_with(REQUEST_ID, {"answer": "Session cookies"})

        # Normal message pipeline should NOT have been called
        ch._on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_pending_question_not_intercepted(self) -> None:
        """A stale pending question (old timestamp) should NOT intercept messages.

        If a pending question file was left behind by a crash, real user
        messages must flow to the normal pipeline instead of being swallowed.
        """
        callback = MagicMock()
        ch = _make_channel(on_ask_user_answer=callback)

        message = self._build_message_ev("hello there")
        # Create a pending question with a very old timestamp (well past timeout)
        stale_pending = _pending_data()
        stale_pending["timestamp"] = "2025-01-01T00:00:00+00:00"

        jid2string_mock = sys.modules["neonize.utils.jid"].Jid2String
        jid2string_mock.return_value = CHAT_JID
        ch._translate_jid = MagicMock(return_value=CHAT_JID)

        with patch(
            "pynchy.chat.plugins.whatsapp.channel.find_pending_for_jid",
            return_value=stale_pending,
        ):
            await ch._handle_message(message)

        # The callback should NOT have been called — stale question is skipped
        callback.assert_not_called()

        # Normal message pipeline SHOULD have been called
        ch._on_message.assert_called_once()
