"""Tests for Discord ask_user interactive widgets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import discord
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


@dataclass
class _UploadedAttachment:
    id: str = "attachment-1"
    filename: str = "screenshot.png"
    url: str = "https://cdn.discord.test/screenshot.png"
    proxy_url: str = "https://media.discord.test/screenshot.png"
    content_type: str = "image/png"
    size: int = 1234
    description: str | None = "Bug screenshot"
    spoiler: bool = False
    ephemeral: bool = True


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
        ).to_runtime_settings(),
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


def _file_upload_question() -> list[dict]:
    return [
        {
            "header": "Evidence",
            "question": "Upload a screenshot of the failure.",
            "fileUpload": {"required": False, "maxFiles": 2},
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
async def test_send_ask_user_uses_a_modal_for_multiple_questions():
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

    view = fake.sends[0][1]["view"]
    assert [item.label for item in view.children] == ["Answer"]
    assert "What should we call it?" in fake.sends[0][0]


@pytest.mark.asyncio
async def test_multiple_question_modal_uses_selects_and_text_inputs():
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    questions = [
        *_single_question(),
        {
            "header": "Name",
            "question": "What should we call it?",
            "options": [],
        },
    ]

    await ch.send_ask_user("discord:direct:42", REQUEST_ID, questions)
    view = fake.sends[0][1]["view"]
    launch_interaction = _interaction()
    await view.children[0].callback(launch_interaction)

    modal = launch_interaction.response.send_modal.call_args.args[0]
    assert [type(child).__name__ for child in modal.children] == ["Label", "Label"]
    select = modal.children[0].component
    text_input = modal.children[1].component
    select._values = ["Option 2"]  # noqa: V101
    text_input._value = "Project Synapse"

    await modal.on_submit(_interaction())

    answer = callback.call_args[0][1]
    assert answer["answer"] == {
        "question_1": "Option 2",
        "question_2": "Project Synapse",
    }


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
    select._values = ["Option 1", "Option 3"]  # noqa: V101
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
async def test_file_upload_modal_submit_delivers_attachment_metadata():
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _file_upload_question())

    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if item.label == "Attach files")
    launch_interaction = _interaction()
    await button.callback(launch_interaction)

    modal = launch_interaction.response.send_modal.call_args.args[0]
    assert isinstance(modal.file_input, discord.ui.FileUpload)
    assert modal.file_input.required is False
    assert modal.file_input.max_values == 2
    modal.file_input._values = [_UploadedAttachment()]  # noqa: V101

    await modal.on_submit(_interaction())

    answer = callback.call_args[0][1]
    assert answer["answer"] == {
        "attachments": [
            {
                "id": "attachment-1",
                "filename": "screenshot.png",
                "url": "https://cdn.discord.test/screenshot.png",
                "proxy_url": "https://media.discord.test/screenshot.png",
                "content_type": "image/png",
                "size": 1234,
                "description": "Bug screenshot",
                "spoiler": False,
                "ephemeral": True,
            }
        ]
    }


@pytest.mark.asyncio
async def test_file_upload_flag_uses_required_single_file_defaults():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_ask_user(
        "discord:direct:42",
        REQUEST_ID,
        [{"question": "Attach evidence", "fileUpload": True}],
    )

    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if item.label == "Attach files")
    interaction = _interaction()
    await button.callback(interaction)

    modal = interaction.response.send_modal.call_args.args[0]
    assert modal.file_input.required is True
    assert modal.file_input.max_values == 1


@pytest.mark.asyncio
async def test_file_upload_button_rejects_an_unauthorized_interaction():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _file_upload_question())
    view = fake.sends[0][1]["view"]
    view.is_interaction_allowed = MagicMock(return_value=False)
    button = next(item for item in view.children if item.label == "Attach files")
    interaction = _interaction()

    await button.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to answer this prompt.", ephemeral=True
    )
    interaction.response.send_modal.assert_not_awaited()


@pytest.mark.asyncio
async def test_multi_question_modal_supports_file_uploads():
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    questions = [
        *_file_upload_question(),
        {"question": "What should we call it?"},
    ]

    await ch.send_ask_user("discord:direct:42", REQUEST_ID, questions)
    view = fake.sends[0][1]["view"]
    launch_interaction = _interaction()
    await view.children[0].callback(launch_interaction)

    modal = launch_interaction.response.send_modal.call_args.args[0]
    file_input = modal.children[0].component
    text_input = modal.children[1].component
    file_input._values = [_UploadedAttachment()]  # noqa: V101
    text_input._value = "Project Synapse"

    await modal.on_submit(_interaction())

    answer = callback.call_args[0][1]
    assert answer["answer"]["question_1"]["attachments"][0]["filename"] == "screenshot.png"
    assert answer["answer"]["question_2"] == "Project Synapse"


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


@pytest.mark.asyncio
async def test_send_ask_user_skips_missing_client_and_non_discord_jid():
    ch = _make_channel()

    assert await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question()) is None

    ch.client = object()
    ch.owns_jid = MagicMock(return_value=False)  # type: ignore[method-assign]
    assert await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question()) is None


@pytest.mark.asyncio
async def test_send_ask_user_returns_none_when_discord_rejects_the_message():
    ch = _make_channel()
    ch.client = object()
    target = MagicMock()
    target.send = AsyncMock(side_effect=discord.DiscordException("message rejected"))
    ch.resolve_channel = AsyncMock(return_value=target)  # type: ignore[method-assign]

    assert await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question()) is None


@pytest.mark.asyncio
async def test_send_ask_user_renders_optionless_questions_without_headers():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]

    await ch.send_ask_user(
        "discord:direct:42",
        REQUEST_ID,
        [{"question": "Use the default?", "options": ["yes"]}],
    )

    assert "- Use the default?" in fake.sends[0][0]
    assert "1. yes" in fake.sends[0][0]


@pytest.mark.asyncio
async def test_select_callback_rejects_an_unauthorized_interaction():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _multi_select_question())
    view = fake.sends[0][1]["view"]
    select = next(item for item in view.children if item.__class__.__name__.endswith("Select"))
    ch.is_interaction_allowed = MagicMock(return_value=False)  # type: ignore[method-assign]

    interaction = _interaction()
    await select.callback(interaction)
    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to answer this prompt.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_select_callback_records_allowed_selection_before_submit():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _multi_select_question())
    view = fake.sends[0][1]["view"]
    select = next(item for item in view.children if item.__class__.__name__.endswith("Select"))
    select._values = ["Option 1", "Option 2"]  # noqa: V101
    interaction = _interaction()

    await select.callback(interaction)

    assert view._selected_answers == ["Option 1", "Option 2"]
    interaction.response.send_message.assert_awaited_once_with(
        "Selection recorded. Press Submit to finish.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_button_callback_rejects_an_unattached_view():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())
    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Option 1")
    view.remove_item(button)

    with pytest.raises(RuntimeError, match="before the view was attached"):
        await button.callback(_interaction())


@pytest.mark.asyncio
async def test_submit_uses_recorded_selection_when_select_control_is_removed():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _multi_select_question())
    view = fake.sends[0][1]["view"]
    select = next(item for item in view.children if item.__class__.__name__.endswith("Select"))
    submit = next(item for item in view.children if getattr(item, "label", None) == "Submit")
    view.record_selected_answers(["Option 1"])
    view.remove_item(select)

    interaction = _interaction()
    await submit.callback(interaction)

    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_requires_a_selection_and_button_rejects_duplicate_answers():
    callback = MagicMock()
    ch = _make_channel(on_ask_user_answer=callback)
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _multi_select_question())
    view = fake.sends[0][1]["view"]
    submit = next(item for item in view.children if getattr(item, "label", None) == "Submit")
    empty_interaction = _interaction()

    await submit.callback(empty_interaction)

    empty_interaction.response.send_message.assert_awaited_once_with(
        "Choose at least one option before submitting.", ephemeral=True
    )

    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())
    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Option 1")
    await button.callback(_interaction())
    duplicate = _interaction()

    await button.callback(duplicate)

    duplicate.response.send_message.assert_awaited_once_with(
        "This prompt has already been answered.", ephemeral=True
    )
    callback.assert_called_once()


@pytest.mark.asyncio
async def test_text_callback_rejects_an_unauthorized_interaction():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _free_text_question())
    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Answer")
    ch.is_interaction_allowed = MagicMock(return_value=False)  # type: ignore[method-assign]
    interaction = _interaction()

    await button.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to answer this prompt.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_form_callback_rejects_an_unauthorized_interaction():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user(
        "discord:direct:42", REQUEST_ID, _single_question() + _free_text_question()
    )
    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Answer")
    ch.is_interaction_allowed = MagicMock(return_value=False)  # type: ignore[method-assign]
    interaction = _interaction()

    await button.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to answer this prompt.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_button_callback_rejects_an_interaction_forbidden_at_finalize():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())
    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Option 1")
    ch.is_interaction_allowed = MagicMock(return_value=False)  # type: ignore[method-assign]
    interaction = _interaction()

    await button.callback(interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "You are not allowed to answer this prompt.", ephemeral=True
    )


@pytest.mark.asyncio
async def test_button_callback_completes_without_an_answer_callback():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())
    view = fake.sends[0][1]["view"]
    button = next(item for item in view.children if getattr(item, "label", None) == "Option 1")
    interaction = _interaction()

    await button.callback(interaction)

    interaction.response.edit_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_ignores_discord_fetch_failure_after_disabling_controls():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeAskUserChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())
    view = fake.sends[0][1]["view"]
    failing_channel = MagicMock()
    failing_channel.fetch_message = AsyncMock(side_effect=discord.DiscordException("gone"))
    ch.resolve_channel = AsyncMock(return_value=failing_channel)  # type: ignore[method-assign]

    await view.on_timeout()

    assert all(item.disabled for item in view.children)


@pytest.mark.asyncio
async def test_timeout_without_a_bound_message_id_skips_discord_fetch():
    ch = _make_channel()
    ch.client = object()
    fake = _FakeSendChannel()
    ch.resolve_channel = AsyncMock(return_value=fake)  # type: ignore[method-assign]
    await ch.send_ask_user("discord:direct:42", REQUEST_ID, _single_question())
    view = fake.sends[0][1]["view"]
    view.bind_message_id(None)  # type: ignore[arg-type]

    await view.on_timeout()

    ch.resolve_channel.assert_awaited_once()
