"""Behavior tests for WhatsApp ask_user delivery and answer ingestion."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

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
        module.__path__ = []  # noqa: V101  # type: ignore[attr-defined]  # import package marker.
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
neonize_utils.enum = neonize_enum  # noqa: V101


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
        self.presence_updates: list[tuple[object, object, object]] = []
        self.reactions: list[tuple[object, object, str, str]] = []
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

    async def send_chat_presence(self, target: object, presence: object, media: object) -> None:
        self.presence_updates.append((target, presence, media))

    async def build_reaction(
        self, chat: object, sender: object, message_id: str, emoji: str
    ) -> object:
        self.reactions.append((chat, sender, message_id, emoji))
        return {"reaction": emoji}

    async def create_group(self, _name: str) -> object:
        return type("Group", (), {"JID": _Jid("new-group@g.us", "new-group", "g.us")})()

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
    extendedTextMessage: _TextPart = field(default_factory=_TextPart)  # noqa: N815 - WhatsApp SDK field name.
    imageMessage: _MediaPart = field(default_factory=_MediaPart)  # noqa: N815 - WhatsApp SDK field name.
    videoMessage: _MediaPart = field(default_factory=_MediaPart)  # noqa: N815 - WhatsApp SDK field name.


@dataclass(frozen=True)
class _InboundEvent:
    Info: _MessageInfo
    Message: _MessageBody


@dataclass(frozen=True)
class _GroupName:
    Name: str


@dataclass(frozen=True)
class _Group:
    JID: _Jid
    GroupName: _GroupName


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

    async def test_set_typing_sends_the_matching_whatsapp_presence(
        self, connected_channel: _ConnectedChannel
    ) -> None:
        await connected_channel.channel.set_typing(CHAT_JID, is_typing=True)
        await connected_channel.channel.set_typing(CHAT_JID, is_typing=False)

        assert connected_channel.client.presence_updates == [
            (("120363001234567890", "g.us"), "composing", "text"),
            (("120363001234567890", "g.us"), "paused", "text"),
        ]

    async def test_send_reaction_builds_and_delivers_the_provider_reaction(
        self, connected_channel: _ConnectedChannel
    ) -> None:
        await connected_channel.channel.send_reaction(
            CHAT_JID, "message-1", "5551234@s.whatsapp.net", "👍"
        )

        assert connected_channel.client.reactions == [
            (("120363001234567890", "g.us"), ("5551234", "s.whatsapp.net"), "message-1", "👍")
        ]
        assert connected_channel.client.sent_messages == [
            (("120363001234567890", "g.us"), {"reaction": "👍"})
        ]

    async def test_create_group_returns_the_provider_group_jid(
        self, connected_channel: _ConnectedChannel
    ) -> None:
        assert await connected_channel.channel.create_group("New Group") == "new-group@g.us"


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


def _use_pending_question_data_dir(data_dir: Path) -> None:
    pending_questions.configure_pending_questions_ipc_base_dir(data_dir / "ipc")


class TestFindPendingForJid:
    def test_finds_matching_jid(self, tmp_path: Path) -> None:
        question_dir = tmp_path / "ipc" / "my-group" / "pending_questions"
        question_dir.mkdir(parents=True)
        (question_dir / f"{REQUEST_ID}.json").write_text(json.dumps(_pending_data()))
        _use_pending_question_data_dir(tmp_path)

        result = pending_questions.find_pending_for_jid(CHAT_JID)

        assert result is not None
        assert result["request_id"] == REQUEST_ID

    def test_ignores_nonmatching_and_error_files(self, tmp_path: Path) -> None:
        question_dir = tmp_path / "ipc" / "my-group" / "pending_questions"
        question_dir.mkdir(parents=True)
        (question_dir / "other.json").write_text(
            json.dumps(_pending_data(chat_jid="different@g.us"))
        )
        error_dir = tmp_path / "ipc" / "errors" / "pending_questions"
        error_dir.mkdir(parents=True)
        (error_dir / f"{REQUEST_ID}.json").write_text(json.dumps(_pending_data()))
        _use_pending_question_data_dir(tmp_path)

        assert pending_questions.find_pending_for_jid(CHAT_JID) is None

    def test_skips_corrupt_files_and_finds_later_match(self, tmp_path: Path) -> None:
        question_dir = tmp_path / "ipc" / "my-group" / "pending_questions"
        question_dir.mkdir(parents=True)
        (question_dir / "corrupt.json").write_text("{bad json")
        (question_dir / f"{REQUEST_ID}.json").write_text(json.dumps(_pending_data()))
        _use_pending_question_data_dir(tmp_path)

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


class TestChatResolutionAndMetadata:
    async def test_resolve_chat_jid_returns_exact_jids_without_a_lookup(
        self, connected_channel: _ConnectedChannel
    ) -> None:
        assert await connected_channel.channel.resolve_chat_jid(CHAT_JID) == CHAT_JID

    @pytest.mark.parametrize(
        ("matches", "expected"),
        [([CHAT_JID], CHAT_JID), ([], None), ([CHAT_JID, "another@g.us"], None)],
    )
    async def test_resolve_chat_jid_requires_one_stored_name_match(
        self,
        tmp_path: Path,
        matches: list[str],
        expected: str | None,
    ) -> None:
        client = _FakeWhatsAppClient()
        channel = _NoopMetadataSyncWhatsAppChannel(
            connection_name="connection.whatsapp.test",
            auth_db_path=str(tmp_path / "neonize.db"),
            assistant_name="pynchy",
            on_message=MagicMock(),
            on_chat_metadata=MagicMock(),
            workspaces=lambda: {CHAT_JID: WORKSPACE},
            client_factory=lambda _auth_db: client,
            find_chat_jids_by_name=AsyncMock(return_value=matches),
        )

        assert await channel.resolve_chat_jid("Project chat") == expected

    async def test_sync_group_metadata_records_named_groups_for_future_resolution(
        self, tmp_path: Path
    ) -> None:
        client = _FakeWhatsAppClient()
        client.get_joined_groups = AsyncMock(
            return_value=[
                _Group(
                    JID=_Jid(CHAT_JID, "120363001234567890", "g.us"),
                    GroupName=_GroupName("Project chat"),
                )
            ]
        )
        update_chat_name = AsyncMock()
        set_last_group_sync = AsyncMock()
        channel = WhatsAppChannel(
            connection_name="connection.whatsapp.test",
            auth_db_path=str(tmp_path / "neonize.db"),
            assistant_name="pynchy",
            on_message=MagicMock(),
            on_chat_metadata=MagicMock(),
            workspaces=lambda: {CHAT_JID: WORKSPACE},
            client_factory=lambda _auth_db: client,
            update_chat_name=update_chat_name,
            set_last_group_sync=set_last_group_sync,
        )

        await channel.sync_group_metadata(force=True)

        update_chat_name.assert_awaited_once_with(CHAT_JID, "Project chat")
        set_last_group_sync.assert_awaited_once()
