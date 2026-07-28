"""Behavior tests for WhatsApp ask_user delivery and answer ingestion."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from pynchy.host.orchestrator.messaging import pending_questions
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)
from pynchy.workspace.api import WorkspaceProfile

if TYPE_CHECKING:
    from pathlib import Path


# Create a typed stand-in for the optional neonize package before importing
# the channel. Real classes keep beartype from treating SDK annotations as
# MagicMock instances.
def _install_module(name: str, *, package: bool = False) -> ModuleType:
    module = ModuleType(name)
    if package:
        module.__path__ = []  # type: ignore[attr-defined]  # noqa: RUF100 - import package marker.
    sys.modules[name] = module
    return module


neonize = _install_module("neonize", package=True)
aioze = _install_module("neonize.aioze", package=True)
aioze_client = _install_module("neonize.aioze.client")
aioze_events = _install_module("neonize.aioze.events")
neonize_events = _install_module("neonize.events")
neonize_utils = _install_module("neonize.utils", package=True)
neonize_jid = _install_module("neonize.utils.jid")
neonize_enum = _install_module("neonize.utils.enum")

neonize.aioze = aioze
aioze.client = aioze_client
aioze.events = aioze_events
neonize.utils = neonize_utils
neonize_utils.jid = neonize_jid
neonize_utils.enum = neonize_enum


class _NeonizeClient:
    pass


class _ConnectedEvent:
    pass


class _ConnectFailureEvent:
    pass


class _DisconnectedEvent:
    pass


class _LoggedOutEvent:
    pass


class _MessageEvent:
    pass


class _PairStatusEvent:
    pass


class _ChatPresence:
    CHAT_PRESENCE_COMPOSING = "composing"
    CHAT_PRESENCE_PAUSED = "paused"


class _ChatPresenceMedia:
    CHAT_PRESENCE_MEDIA_TEXT = "text"


aioze_client.NewAClient = _NeonizeClient
neonize_events.ConnectedEv = _ConnectedEvent
neonize_events.ConnectFailureEv = _ConnectFailureEvent
neonize_events.DisconnectedEv = _DisconnectedEvent
neonize_events.LoggedOutEv = _LoggedOutEvent
neonize_events.MessageEv = _MessageEvent
neonize_events.PairStatusEv = _PairStatusEvent
neonize_enum.ChatPresence = _ChatPresence
neonize_enum.ChatPresenceMedia = _ChatPresenceMedia
neonize_jid.Jid2String = lambda jid: getattr(jid, "value", "")
neonize_jid.build_jid = lambda *parts: parts

from pynchy.plugins.channels.whatsapp import (  # noqa: E402
    WhatsAppChannel,
    resolve_ask_user_answer,
)
from pynchy.plugins.channels.whatsapp import channel as whatsapp_channel  # noqa: E402

CHAT_JID = "120363001234567890@g.us"
REQUEST_ID = "req-wa-test-001"
WORKSPACE = WorkspaceProfile(
    jid=CHAT_JID,
    name="Test chat",
    folder="test-group",
    trigger="always",
    added_at="2024-01-01",
)


def _questions_with_options() -> list[dict[str, Any]]:
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


def _questions_with_string_options() -> list[dict[str, Any]]:
    return [{"question": "Pick a color", "options": ["Red", "Green", "Blue"]}]


def _questions_without_options() -> list[dict[str, Any]]:
    return [{"question": "What is the project name?"}]


def _pending_data(
    *,
    chat_jid: str = CHAT_JID,
    request_id: str = REQUEST_ID,
    questions: list[dict[str, Any]] | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "chat_jid": chat_jid,
        "questions": questions or _questions_with_options(),
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
    }


class _FakeEventRegistry:
    def __init__(self) -> None:
        self.handlers: dict[object, Any] = {}
        self.qr_handler: Any = None

    def __call__(self, event_type: object) -> Any:
        def register(handler: Any) -> Any:
            self.handlers[event_type] = handler
            return handler

        return register

    def qr(self, handler: Any) -> Any:
        self.qr_handler = handler
        return handler


class _FakeWhatsAppClient:
    """Small SDK-shaped client driven through WhatsAppChannel's public API."""

    def __init__(self) -> None:
        self.event = _FakeEventRegistry()
        self.me: object | None = None
        self.sent_messages: list[tuple[object, object]] = []
        self._idle = asyncio.Event()

    async def connect(self) -> None:
        handler = self.event.handlers[whatsapp_channel.ConnectedEv]
        await handler(self, object())

    async def idle(self) -> None:
        await self._idle.wait()

    async def disconnect(self) -> None:
        self._idle.set()

    async def send_message(self, target: object, text: object) -> None:
        self.sent_messages.append((target, text))

    async def get_joined_groups(self) -> list[object]:
        return []


class _NoopMetadataSyncWhatsAppChannel(WhatsAppChannel):
    async def sync_group_metadata(self, *, force: bool = False) -> None:
        _ = force


@dataclass
class _ConnectedChannel:
    channel: WhatsAppChannel
    client: _FakeWhatsAppClient
    on_message: MagicMock
    on_metadata: MagicMock
    on_answer: MagicMock


@pytest.fixture
async def connected_channel(tmp_path: Path) -> _ConnectedChannel:
    client = _FakeWhatsAppClient()
    on_message = MagicMock()
    on_metadata = MagicMock()
    on_answer = MagicMock()
    channel = _NoopMetadataSyncWhatsAppChannel(
        connection_name="connection.whatsapp.test",
        auth_db_path=str(tmp_path / "neonize.db"),
        assistant_name="pynchy",
        on_message=on_message,
        on_chat_metadata=on_metadata,
        workspaces=lambda: {CHAT_JID: WORKSPACE},
        on_ask_user_answer=on_answer,
        client_factory=lambda _auth_db: client,
    )
    await channel.connect()
    try:
        yield _ConnectedChannel(channel, client, on_message, on_metadata, on_answer)
    finally:
        await channel.disconnect()


@dataclass(frozen=True)
class _Jid:
    value: str
    User: str
    Server: str


@dataclass(frozen=True)
class _MessageSource:
    Chat: _Jid
    Sender: _Jid
    IsFromMe: bool


@dataclass(frozen=True)
class _MessageInfo:
    MessageSource: _MessageSource
    Timestamp: int
    ID: str
    Pushname: str


@dataclass(frozen=True)
class _TextPart:
    text: str = ""


@dataclass(frozen=True)
class _MediaPart:
    caption: str = ""


@dataclass(frozen=True)
class _MessageBody:
    conversation: str
    extendedTextMessage: _TextPart = field(default_factory=_TextPart)  # noqa: N815, RUF100 - WhatsApp SDK field name.
    imageMessage: _MediaPart = field(default_factory=_MediaPart)  # noqa: N815, RUF100 - WhatsApp SDK field name.
    videoMessage: _MediaPart = field(default_factory=_MediaPart)  # noqa: N815, RUF100 - WhatsApp SDK field name.


@dataclass(frozen=True)
class _InboundEvent:
    Info: _MessageInfo
    Message: _MessageBody


def _inbound_event(content: str, *, is_from_me: bool = False) -> _InboundEvent:
    chat = _Jid(value=CHAT_JID, User="120363001234567890", Server="g.us")
    sender = _Jid(value="5551234@s.whatsapp.net", User="5551234", Server="s.whatsapp.net")
    return _InboundEvent(
        Info=_MessageInfo(
            MessageSource=_MessageSource(Chat=chat, Sender=sender, IsFromMe=is_from_me),
            Timestamp=1_740_000_000,
            ID="msg-123",
            Pushname="Test User",
        ),
        Message=_MessageBody(conversation=content),
    )


def _rendered_text(client: _FakeWhatsAppClient) -> str:
    assert len(client.sent_messages) == 1
    text = client.sent_messages[0][1]
    assert isinstance(text, str)
    return text


class TestSendAskUser:
    async def test_formats_numbered_text(self, connected_channel: _ConnectedChannel) -> None:
        await connected_channel.channel.send_ask_user(
            CHAT_JID, REQUEST_ID, _questions_with_options()
        )

        text = _rendered_text(connected_channel.client)
        assert "Which auth strategy?" in text
        assert "1. JWT tokens" in text
        assert "2. Session cookies" in text
        assert "3. OAuth 2.0" in text
        assert "Reply with a number" in text

    async def test_formats_string_options(self, connected_channel: _ConnectedChannel) -> None:
        await connected_channel.channel.send_ask_user(
            CHAT_JID, REQUEST_ID, _questions_with_string_options()
        )

        text = _rendered_text(connected_channel.client)
        assert "1. Red" in text
        assert "2. Green" in text
        assert "3. Blue" in text

    async def test_formats_free_text_prompt(self, connected_channel: _ConnectedChannel) -> None:
        await connected_channel.channel.send_ask_user(
            CHAT_JID, REQUEST_ID, _questions_without_options()
        )

        text = _rendered_text(connected_channel.client)
        assert "What is the project name?" in text
        assert "Reply with your answer." in text
        assert "1." not in text

    async def test_returns_request_id_and_delivers_one_message(
        self, connected_channel: _ConnectedChannel
    ) -> None:
        result = await connected_channel.channel.send_ask_user(
            CHAT_JID, REQUEST_ID, _questions_with_options()
        )

        assert result == REQUEST_ID
        assert len(connected_channel.client.sent_messages) == 1

    async def test_send_event_renders_and_delivers_text(
        self, connected_channel: _ConnectedChannel
    ) -> None:
        await connected_channel.channel.send_event(
            CHAT_JID,
            OutboundEvent(type=OutboundEventType.TEXT, content="Hello world"),
        )

        assert _rendered_text(connected_channel.client) == "Hello world"


class TestResolveAskUserAnswer:
    @pytest.mark.parametrize(
        ("reply", "questions", "expected"),
        [
            ("2", _questions_with_options(), {"answer": "Session cookies"}),
            ("1", _questions_with_string_options(), {"answer": "Red"}),
            ("99", _questions_with_options(), {"answer": "99"}),
            ("0", _questions_with_options(), {"answer": "0"}),
            (
                "I want something else",
                _questions_with_options(),
                {"answer": "I want something else"},
            ),
            ("  1  ", _questions_with_options(), {"answer": "JWT tokens"}),
            ("hello", [], {"answer": "hello"}),
            ("²", _questions_with_options(), {"answer": "²"}),
        ],
    )
    def test_resolves_numbered_or_free_text_answer(
        self,
        reply: str,
        questions: list[dict[str, Any]],
        expected: dict[str, str],
    ) -> None:
        assert resolve_ask_user_answer(reply, questions) == expected


@dataclass(frozen=True)
class _Settings:
    data_dir: Path


def _use_pending_question_data_dir(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    monkeypatch.setattr(pending_questions, "get_settings", lambda: _Settings(data_dir))


class TestFindPendingForJid:
    def test_finds_matching_jid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        question_dir = tmp_path / "ipc" / "my-group" / "pending_questions"
        question_dir.mkdir(parents=True)
        (question_dir / f"{REQUEST_ID}.json").write_text(json.dumps(_pending_data()))
        _use_pending_question_data_dir(monkeypatch, tmp_path)

        result = pending_questions.find_pending_for_jid(CHAT_JID)

        assert result is not None
        assert result["request_id"] == REQUEST_ID

    def test_ignores_nonmatching_and_error_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        question_dir = tmp_path / "ipc" / "my-group" / "pending_questions"
        question_dir.mkdir(parents=True)
        (question_dir / "other.json").write_text(
            json.dumps(_pending_data(chat_jid="different@g.us"))
        )
        error_dir = tmp_path / "ipc" / "errors" / "pending_questions"
        error_dir.mkdir(parents=True)
        (error_dir / f"{REQUEST_ID}.json").write_text(json.dumps(_pending_data()))
        _use_pending_question_data_dir(monkeypatch, tmp_path)

        assert pending_questions.find_pending_for_jid(CHAT_JID) is None

    def test_skips_corrupt_files_and_finds_later_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        question_dir = tmp_path / "ipc" / "my-group" / "pending_questions"
        question_dir.mkdir(parents=True)
        (question_dir / "corrupt.json").write_text("{bad json")
        (question_dir / f"{REQUEST_ID}.json").write_text(json.dumps(_pending_data()))
        _use_pending_question_data_dir(monkeypatch, tmp_path)

        result = pending_questions.find_pending_for_jid(CHAT_JID)

        assert result is not None
        assert result["request_id"] == REQUEST_ID


class TestInboundMessageAdapter:
    async def test_pending_answer_is_intercepted(
        self,
        connected_channel: _ConnectedChannel,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(whatsapp_channel, "Jid2String", lambda jid: jid.value)
        monkeypatch.setattr(whatsapp_channel, "find_pending_for_jid", lambda _jid: _pending_data())

        await connected_channel.channel.ingest_inbound_message(_inbound_event("2"))

        connected_channel.on_answer.assert_called_once_with(
            REQUEST_ID, {"answer": "Session cookies"}
        )
        connected_channel.on_message.assert_not_called()

    async def test_stale_question_reaches_normal_message_pipeline(
        self,
        connected_channel: _ConnectedChannel,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(whatsapp_channel, "Jid2String", lambda jid: jid.value)
        monkeypatch.setattr(
            whatsapp_channel,
            "find_pending_for_jid",
            lambda _jid: _pending_data(timestamp="2025-01-01T00:00:00+00:00"),
        )

        await connected_channel.channel.ingest_inbound_message(_inbound_event("hello there"))

        connected_channel.on_answer.assert_not_called()
        delivered = connected_channel.on_message.call_args.args[1]
        assert delivered.content == "hello there"

    async def test_own_agent_echo_is_not_reemitted(
        self,
        connected_channel: _ConnectedChannel,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(whatsapp_channel, "Jid2String", lambda jid: jid.value)
        await connected_channel.channel.ingest_inbound_message(
            _inbound_event("pynchy: working on it", is_from_me=True)
        )

        connected_channel.on_answer.assert_not_called()
        connected_channel.on_message.assert_not_called()


class TestCallbackSurface:
    def test_exposes_answer_callback(self, connected_channel: _ConnectedChannel) -> None:
        assert connected_channel.channel.on_ask_user_answer is connected_channel.on_answer
