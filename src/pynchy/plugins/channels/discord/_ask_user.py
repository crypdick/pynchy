"""Discord ask_user widgets and helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import discord

from pynchy.logger import logger

from ._ask_user_file_upload import (
    AskUserFileButton,
    _attachment_metadata,
    _file_upload_settings,
)

if TYPE_CHECKING:
    from ._channel import DiscordChannel
else:
    DiscordChannel = object

_ASK_USER_PREFIX = "au"
_MAX_BUTTONS_PER_ROW = 5
_MAX_BUTTONS_TOTAL = 25
_TEXT_MODAL_CUSTOM_ID = f"{_ASK_USER_PREFIX}:text"
_SELECT_CUSTOM_ID = f"{_ASK_USER_PREFIX}:select"
_FORM_MODAL_CUSTOM_ID = f"{_ASK_USER_PREFIX}:form"
_ASK_USER_VIEW_MISSING = "Discord ask_user callback called before the view was attached"


def _require_ask_user_view(view: discord.ui.View | None) -> DiscordAskUserView:
    if view is None:
        raise RuntimeError(_ASK_USER_VIEW_MISSING)
    return cast("DiscordAskUserView", view)


def build_ask_user_text(questions: list[dict[str, Any]]) -> str:
    """Render a readable ask_user body message."""
    lines: list[str] = ["**Question:**"]
    for question in questions:
        header = question.get("header", "")
        prompt = question.get("question", "")
        if header:
            lines.append(f"**{header}:** {prompt}")
        else:
            lines.append(f"- {prompt}")
        options = question.get("options", [])
        for idx, option in enumerate(options, 1):
            label = option.get("label", str(option)) if isinstance(option, dict) else str(option)
            lines.append(f"  {idx}. {label}")
        if (upload := _file_upload_settings(question)) is not None:
            _, max_files = upload
            noun = "file" if max_files == 1 else "files"
            lines.append(f"  Attach up to {max_files} {noun} with Answer.")
    return "\n".join(lines)


def supports_interactive_ask_user(questions: list[dict[str, Any]]) -> bool:
    """Return True when this prompt shape fits Discord's message or modal limits."""
    if not questions or len(questions) > 4:
        return False
    if len(questions) > 1:
        return all(
            _file_upload_settings(question) is not None or len(question.get("options", [])) <= 25
            for question in questions
        )
    question = questions[0]
    if _file_upload_settings(question) is not None:
        return True
    options = question.get("options", [])
    return not options or len(options) <= _MAX_BUTTONS_TOTAL


async def send_ask_user_prompt(
    channel: DiscordChannel,
    jid: str,
    request_id: str,
    questions: list[dict[str, object]],
) -> str | None:
    """Post an ask_user prompt, using buttons when the prompt shape fits."""
    if channel.client is None or not channel.owns_jid(jid):
        return None
    text = build_ask_user_text(questions)
    view: DiscordAskUserView | None = None
    if supports_interactive_ask_user(questions):
        view = DiscordAskUserView(
            channel=channel,
            jid=jid,
            request_id=request_id,
            questions=questions,
        )
    try:
        target = await channel.resolve_channel(jid)
        message = await target.send(
            text,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
            suppress_embeds=True,
        )
    except discord.DiscordException as exc:
        logger.warning("Discord ask_user failed", err=str(exc))
        return None
    if view is not None:
        view.bind_message_id(str(message.id))
        channel.bind_ask_user_view(str(message.id), view)
    return f"discord-{message.id}"


def encode_button_custom_id(request_id: str, option_index: int) -> str:
    """Build a compact button custom ID."""
    return f"{_ASK_USER_PREFIX}:{request_id}:{option_index}"


class AskUserButton(discord.ui.Button["DiscordAskUserView"]):
    """Simple button that resolves to one labeled answer."""

    def __init__(self, *, request_id: str, option_index: int, label: str, row: int) -> None:
        super().__init__(
            label=label,
            custom_id=encode_button_custom_id(request_id, option_index),
            row=row,
        )
        self._answer = label

    async def callback(self, interaction: object) -> None:
        view = _require_ask_user_view(self.view)
        await view.finalize_answer(cast("Any", interaction), self._answer)


class AskUserSelect(discord.ui.Select["DiscordAskUserView"]):
    """Dropdown/select control for single- or multi-select questions."""

    def __init__(self, *, question: dict[str, Any]) -> None:
        options = question.get("options", [])
        multi_select = bool(question.get("multiSelect"))
        super().__init__(
            custom_id=_SELECT_CUSTOM_ID,
            placeholder=question.get("header") or "Select an answer",
            min_values=1,
            max_values=len(options) if multi_select else 1,
            options=[
                discord.SelectOption(
                    label=(opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)),
                    value=(opt.get("label", str(opt)) if isinstance(opt, dict) else str(opt)),
                    description=(opt.get("description") if isinstance(opt, dict) else None),
                )
                for opt in options
            ],
        )

    async def callback(self, interaction: object) -> None:
        interaction_api = cast("Any", interaction)
        view = _require_ask_user_view(self.view)
        if not view.is_interaction_allowed(interaction_api):
            await interaction_api.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return
        view.record_selected_answers(self.values)
        await interaction_api.response.send_message(
            "Selection recorded. Press Submit to finish.",
            ephemeral=True,
        )


class AskUserSubmitButton(discord.ui.Button["DiscordAskUserView"]):
    """Submit button for select-based prompts."""

    def __init__(self) -> None:
        super().__init__(label="Submit", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: object) -> None:
        view = _require_ask_user_view(self.view)
        await view.submit_selected_answers(cast("Any", interaction))


class AskUserTextButton(discord.ui.Button["DiscordAskUserView"]):
    """Launch a modal for free-text answers."""

    def __init__(self) -> None:
        super().__init__(
            label="Answer",
            style=discord.ButtonStyle.primary,
            custom_id=_TEXT_MODAL_CUSTOM_ID,
        )

    async def callback(self, interaction: object) -> None:
        interaction_api = cast("Any", interaction)
        view = _require_ask_user_view(self.view)
        if not view.is_interaction_allowed(interaction_api):
            await interaction_api.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return
        await interaction_api.response.send_modal(AskUserTextModal(view))


class AskUserFormButton(discord.ui.Button["DiscordAskUserView"]):
    """Launch a modal for a multi-question ask_user prompt."""

    def __init__(self) -> None:
        super().__init__(
            label="Answer",
            style=discord.ButtonStyle.primary,
            custom_id=_FORM_MODAL_CUSTOM_ID,
        )

    async def callback(self, interaction: object) -> None:
        interaction_api = cast("Any", interaction)
        view = _require_ask_user_view(self.view)
        if not view.is_interaction_allowed(interaction_api):
            await interaction_api.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return
        await interaction_api.response.send_modal(AskUserFormModal(view))


class AskUserTextModal(discord.ui.Modal):
    """Modal prompt for free-text ask_user answers."""

    def __init__(self, view: DiscordAskUserView) -> None:
        question = view.primary_question()
        raw_header = question.get("header")
        header = raw_header if isinstance(raw_header, str) else "Answer question"
        super().__init__(title=header[:45], custom_id=_TEXT_MODAL_CUSTOM_ID)
        self._view = view
        raw_prompt = question.get("question", "")
        prompt = raw_prompt if isinstance(raw_prompt, str) else str(raw_prompt)
        self.answer_input: Any = discord.ui.TextInput(
            custom_id=f"{_TEXT_MODAL_CUSTOM_ID}:input",
            placeholder="Type your answer...",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(discord.ui.Label(text=(prompt[:45] or "Answer"), component=self.answer_input))

    async def on_submit(self, interaction: object) -> None:  # noqa: V105
        await self._view.finalize_answer(cast("Any", interaction), self.answer_input.value or "")


class AskUserFormModal(discord.ui.Modal):
    """Modal containing one select or text input for each ask_user question."""

    def __init__(self, view: DiscordAskUserView) -> None:
        super().__init__(title="Answer questions", custom_id=_FORM_MODAL_CUSTOM_ID)
        self._view = view
        self._fields: list[tuple[str, Any]] = []
        for index, question in enumerate(view.questions):
            key = f"question_{index + 1}"
            prompt = str(question.get("question", "Question"))[:45] or key
            options = question.get("options", [])
            component: discord.ui.Item[Any]
            upload_settings = _file_upload_settings(question)
            if upload_settings is not None:
                required, max_files = upload_settings
                component = discord.ui.FileUpload(
                    custom_id=f"{_FORM_MODAL_CUSTOM_ID}:{index}",
                    required=required,
                    min_values=1 if required else 0,
                    max_values=max_files,
                )
            elif options:
                component = discord.ui.Select(
                    custom_id=f"{_FORM_MODAL_CUSTOM_ID}:{index}",
                    placeholder="Select an answer",
                    min_values=1,
                    max_values=len(options) if question.get("multiSelect") else 1,
                    options=[
                        discord.SelectOption(
                            label=(
                                option.get("label", str(option))
                                if isinstance(option, dict)
                                else str(option)
                            )[:100],
                            value=(
                                option.get("label", str(option))
                                if isinstance(option, dict)
                                else str(option)
                            )[:100],
                            description=(
                                option.get("description", "")[:100]
                                if isinstance(option, dict)
                                and isinstance(option.get("description"), str)
                                else None
                            ),
                        )
                        for option in options
                    ],
                )
            else:
                component = discord.ui.TextInput(
                    custom_id=f"{_FORM_MODAL_CUSTOM_ID}:{index}",
                    placeholder="Type your answer...",
                    required=True,
                    style=discord.TextStyle.paragraph,
                    max_length=4000,
                )
            self._fields.append((key, component))
            self.add_item(discord.ui.Label(text=prompt, component=component))

    @staticmethod
    def _value(component: object) -> str | list[str] | dict[str, list[dict[str, object]]]:
        if isinstance(component, discord.ui.TextInput):
            return component.value or ""
        if isinstance(component, discord.ui.FileUpload):
            return {"attachments": [_attachment_metadata(item) for item in component.values]}
        select = cast("discord.ui.Select[discord.ui.View]", component)
        values = list(select.values)
        return values if select.max_values > 1 else (values[0] if values else "")

    async def on_submit(self, interaction: object) -> None:  # noqa: V105
        answers = {key: self._value(component) for key, component in self._fields}
        await self._view.finalize_answer(cast("Any", interaction), answers)


class DiscordAskUserView(discord.ui.View):
    """Transient Discord view for an ask_user prompt."""

    def __init__(
        self,
        *,
        channel: DiscordChannel,
        jid: str,
        request_id: str,
        questions: list[dict[str, Any]],
        timeout: float = 900.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._channel = channel
        self._jid = jid
        self._request_id = request_id
        self._questions = questions
        self._message_id: str | None = None
        self._completed = False
        self._selected_answers: list[str] = []

        if len(questions) > 1:
            self.add_item(AskUserFormButton())
            return

        question = questions[0]
        if _file_upload_settings(question) is not None:
            self.add_item(AskUserFileButton())
            return
        options = question.get("options", [])
        if not options:
            self.add_item(AskUserTextButton())
        elif question.get("multiSelect", False):
            self.add_item(AskUserSelect(question=question))
            self.add_item(AskUserSubmitButton())
        else:
            for idx, option in enumerate(options):
                label = (
                    option.get("label", str(option)) if isinstance(option, dict) else str(option)
                )
                self.add_item(
                    AskUserButton(
                        request_id=request_id,
                        option_index=idx,
                        label=label,
                        row=idx // _MAX_BUTTONS_PER_ROW,
                    )
                )
            if len(options) <= (_MAX_BUTTONS_TOTAL - 1) and not question.get("skill_access"):
                self.add_item(AskUserTextButton())

    def bind_message_id(self, message_id: str | None) -> None:
        self._message_id = message_id

    def primary_question(self) -> dict[str, Any]:
        return self._questions[0]

    @property
    def questions(self) -> list[dict[str, Any]]:
        return self._questions

    def record_selected_answers(self, answers: list[str]) -> None:
        self._selected_answers = list(answers)

    def _question_prompt(self) -> str:
        prompt = self._questions[0].get("question", "")
        return prompt if isinstance(prompt, str) else str(prompt)

    def _current_selected_answers(self) -> list[str]:
        for child in self.children:
            if isinstance(child, AskUserSelect):
                return list(child.values)
        return list(self._selected_answers)

    async def submit_selected_answers(self, interaction: object) -> None:
        interaction_api = cast("Any", interaction)
        selected_answers = self._current_selected_answers()
        if not selected_answers:
            await interaction_api.response.send_message(
                "Choose at least one option before submitting.",
                ephemeral=True,
            )
            return
        await self.finalize_answer(interaction_api, selected_answers)

    async def finalize_answer(self, interaction: object, answer_value: object) -> None:
        interaction_api = cast("Any", interaction)
        if not self.is_interaction_allowed(interaction_api):
            await interaction_api.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return

        if self._completed:
            await interaction_api.response.send_message(
                "This prompt has already been answered.",
                ephemeral=True,
            )
            return

        self._completed = True
        answer: dict[str, object] = {
            "answer": answer_value,
            "answered_by": str(interaction_api.user.id),
            "channel_id": (
                str(interaction_api.channel.id) if interaction_api.channel is not None else ""
            ),
            "message_id": f"discord-{self._message_id}" if self._message_id else None,
        }
        if self._channel.on_ask_user_answer is not None:
            self._channel.on_ask_user_answer(self._request_id, answer)

        prompt = self._question_prompt()
        if isinstance(answer_value, list):
            answer_text = ", ".join(answer_value)
        elif isinstance(answer_value, dict):
            answer_text = "; ".join(f"{key}: {value}" for key, value in answer_value.items())
        else:
            answer_text = str(answer_value)
        await interaction_api.response.edit_message(
            content=f"**Question:** {prompt}\n**Answer:** {answer_text}",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._forget()
        self.stop()

    async def on_timeout(self) -> None:  # noqa: V105
        if self._message_id is not None:
            for item in self.children:
                item.disabled = True  # noqa: V101
            try:
                channel = await self._channel.resolve_channel(self._jid)
                message = await channel.fetch_message(int(self._message_id))
                await message.edit(
                    content=(
                        f"**Question:** {self._question_prompt()}\n"
                        "_This prompt expired before anyone answered._"
                    ),
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.DiscordException:
                pass
        self._forget()

    def _forget(self) -> None:
        if self._message_id is not None:
            self._channel.forget_ask_user_view(self._message_id)

    def is_interaction_allowed(self, interaction: object) -> bool:
        return self._channel.is_interaction_allowed(interaction)
