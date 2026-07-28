"""Tests for Discord ask_user interactive widgets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.config.api import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel

DISCORD_BOT_ENV = "X"
DISCORD_BOT_VALUE = "token"

REQUEST_ID = "req-discord-123"


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)


class _FakeSendChannel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []
        self._next_id = 100

    async def send(self, content: str, **kwargs) -> _FakeMessage:
        self._next_id += 1
        self.sends.append((content, kwargs))
        return _FakeMessage(self._next_id)

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        for _content, _kwargs in reversed(self.sends):
            # each send returns a message with incrementing ids, so recreate lookup
            pass
        raise KeyError(message_id)


class _FakeAskUserChannel:
    def __init__(self) -> None:
        self.sends: list[tuple[str, dict]] = []
        self.messages: dict[int, _FakeMessage] = {}
        self._next_id = 100

    async def send(self, content: str, **kwargs) -> _FakeMessage:
        self._next_id += 1
        message = _FakeMessage(self._next_id)
        self.messages[message.id] = message
        self.sends.append((content, kwargs))
        return message

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        return self.messages[message_id]


@dataclass
class _InteractionUser:
    id: str
    bot: bool
    roles: list[object]


@dataclass
class _InteractionChannel:
    id: str


@dataclass
class _InteractionResponse:
    edit_message: AsyncMock
    send_message: AsyncMock
    send_modal: AsyncMock


@dataclass
class _Interaction:
    """The Discord interaction surface used by the ask_user adapter."""

    user: _InteractionUser
    guild: object | None
    channel: _InteractionChannel
    response: _InteractionResponse


def _interaction() -> _Interaction:
    return _Interaction(
        user=_InteractionUser(id="42", bot=False, roles=[]),
        guild=None,
        channel=_InteractionChannel(id="dm-1"),
        response=_InteractionResponse(
            edit_message=AsyncMock(),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
        ),
    )


def _make_channel(*, on_ask_user_answer: object | None = None) -> DiscordChannel:
    return DiscordChannel(
        connection_name="connection.discord.test",
        config=DiscordConnectionConfig(
            bot_token_env=DISCORD_BOT_ENV, dm_policy="open", group_policy="disabled"
        ),
        bot_token=DISCORD_BOT_VALUE,
        on_message=lambda jid, msg: None,
        on_chat_metadata=lambda jid, ts, name: None,
        audio_cache_dir=Path("data/media/discord"),
        on_ask_user_answer=on_ask_user_answer,
    )


def _single_question(*, count: int = 2) -> list[dict]:
    options = [
        {"label": f"Option {idx}", "description": f"Choice {idx}"} for idx in range(1, count + 1)
    ]
    return [
        {
            "header": "Framework",
            "question": "Which framework should we use?",
            "options": options,
            "multiSelect": False,
        }
    ]


def _multi_select_question() -> list[dict]:
    question = _single_question(count=3)[0]
    question["multiSelect"] = True
    return [question]


def _skill_access_question() -> list[dict]:
    question = _single_question(count=4)[0]
    question["skill_access"] = {"skill_name": "obsidian-knowledge"}
    return [question]


def _free_text_question() -> list[dict]:
    return [
        {
            "header": "Name",
            "question": "What should we call it?",
            "options": [],
            "multiSelect": False,
        }
    ]


@pytest.mark.asyncio
async def test_send_ask_user_posts_button_view_for_single_question():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    message_id = await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())

    assert message_id == "discord-101"
    assert len(fake.sends) == 1
    content, kwargs = fake.sends[0]
    assert "Which framework should we use?" in content
    assert kwargs["view"] is not None
    labels = [item.label for item in kwargs["view"].children if getattr(item, "label", None)]
    assert labels[:2] == ["Option 1", "Option 2"]


@pytest.mark.asyncio
async def test_send_ask_user_splits_more_than_five_buttons_across_rows():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question(count=6))

    view = fake.sends[0][1]["view"]
    rows = [item.row for item in view.children if getattr(item, "label", None)]
    assert rows.count(0) == 5
    assert rows.count(1) == 1


@pytest.mark.asyncio
async def test_send_ask_user_falls_back_to_text_for_multiple_questions():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    questions = [
        *_single_question(),
        {
            "header": "Name",
            "question": "What should we call it?",
            "options": [],
            "multiSelect": False,
        },
    ]

    await ch.send_ask_user("discord:direct:42", REQUEST_ID, questions)

    assert fake.sends[0][1]["view"] is None
    assert "What should we call it?" in fake.sends[0][0]


@pytest.mark.asyncio
async def test_send_ask_user_uses_select_for_multi_select_question():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _multi_select_question())

    view = fake.sends[0][1]["view"]
    select = next(item for item in view.children if item.__class__.__name__.endswith("Select"))
    submit = next(item for item in view.children if getattr(item, "label", None) == "Submit")
    assert select.max_values == 3
    assert submit is not None


@pytest.mark.asyncio
async def test_skill_access_prompt_has_only_policy_buttons():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _skill_access_question())

    view = fake.sends[0][1]["view"]
    labels = [item.label for item in view.children if getattr(item, "label", None)]
    assert labels == ["Option 1", "Option 2", "Option 3", "Option 4"]


@pytest.mark.asyncio
async def test_multi_select_submit_delivers_list_answer():
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _multi_select_question())

    view = fake.sends[0][1]["view"]
    select = next(item for item in view.children if item.__class__.__name__.endswith("Select"))
    submit = next(item for item in view.children if getattr(item, "label", None) == "Submit")
    select._values = ["Option 1", "Option 3"]
    interaction = _interaction()

    await submit.callback(interaction)

    answer = callback.call_args[0][1]
    assert answer["answer"] == ["Option 1", "Option 3"]


@pytest.mark.asyncio
async def test_send_ask_user_uses_modal_launcher_for_free_text_question():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _free_text_question())

    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Answer")
    assert button is not None


@pytest.mark.asyncio
async def test_free_text_modal_submit_delivers_answer():
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _free_text_question())

    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Answer")
    launch_interaction = _interaction()

    await button.callback(launch_interaction)

    modal = launch_interaction.response.send_modal.call_args.args[0]
    modal.answer_input._value = "Project Synapse"
    submit_interaction = _interaction()

    await modal.on_submit(submit_interaction)

    answer = callback.call_args[0][1]
    assert answer["answer"] == "Project Synapse"


@pytest.mark.asyncio
async def test_button_callback_delivers_answer_and_removes_interactivity():
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())

    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Option 1")
    interaction = _interaction()

    await button.callback(interaction)

    callback.assert_called_once()
    assert callback.call_args[0][0] == REQUEST_ID
    answer = callback.call_args[0][1]
    assert answer["answer"] == "Option 1"
    assert answer["answered_by"] == "42"
    assert answer["channel_id"] == "dm-1"
    assert answer["message_id"] == "discord-101"
    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_disables_interactivity_and_marks_prompt_expired():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeAskUserChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())

    view = fake.sends[0][1]["view"]

    await view.on_timeout()

    message = await fake.fetch_message(101)
    assert message.edits, "expected timed-out message to be edited"
    assert "expired" in message.edits[-1]["content"].lower()
    expired_view = message.edits[-1]["view"]
    assert all(item.disabled for item in expired_view.children)
