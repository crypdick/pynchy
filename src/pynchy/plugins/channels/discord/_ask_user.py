"""Discord ask_user widgets and helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from ._access import InboundContext

if TYPE_CHECKING:
    from ._channel import DiscordChannel
else:
    DiscordChannel = object

_ASK_USER_PREFIX = "au"
_MAX_BUTTONS_PER_ROW = 5
_MAX_BUTTONS_TOTAL = 25
_TEXT_MODAL_CUSTOM_ID = f"{_ASK_USER_PREFIX}:text"
_SELECT_CUSTOM_ID = f"{_ASK_USER_PREFIX}:select"


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
    return "\n".join(lines)


def supports_interactive_ask_user(questions: list[dict[str, Any]]) -> bool:
    """Return True when this prompt shape fits the current Discord widget slice."""
    if len(questions) != 1:
        return False
    question = questions[0]
    options = question.get("options", [])
    return not options or len(options) <= _MAX_BUTTONS_TOTAL


def encode_button_custom_id(request_id: str, option_index: int) -> str:
    """Build a compact button custom ID."""
    return f"{_ASK_USER_PREFIX}:{request_id}:{option_index}"


@dataclass(frozen=True)
class DecodedButtonId:
    request_id: str
    option_index: int


def decode_button_custom_id(custom_id: str) -> DecodedButtonId | None:
    """Parse a button custom ID back into its routing payload."""
    prefix, sep, remainder = custom_id.partition(":")
    if prefix != _ASK_USER_PREFIX or not sep:
        return None
    request_id, sep, raw_index = remainder.rpartition(":")
    if not request_id or not sep:
        return None
    try:
        return DecodedButtonId(request_id=request_id, option_index=int(raw_index))
    except ValueError:
        return None


class AskUserButton(discord.ui.Button["DiscordAskUserView"]):
    """Simple button that resolves to one labeled answer."""

    def __init__(self, *, request_id: str, option_index: int, label: str, row: int) -> None:
        super().__init__(
            label=label,
            custom_id=encode_button_custom_id(request_id, option_index),
            row=row,
        )
        self._answer = label

    async def callback(self, interaction: Any) -> None:
        assert self.view is not None
        await self.view.finalize_answer(interaction, self._answer)


class AskUserSelect(discord.ui.Select["DiscordAskUserView"]):
    """Dropdown/select control for single- or multi-select questions."""

    def __init__(self, *, question: dict[str, Any]) -> None:
        options = question.get("options", [])
        multi_select = bool(question.get("multiSelect", False))
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

    async def callback(self, interaction: Any) -> None:
        assert self.view is not None
        if not self.view._interaction_allowed(interaction):
            await interaction.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return
        self.view._selected_answers = list(self.values)
        await interaction.response.send_message(
            "Selection recorded. Press Submit to finish.",
            ephemeral=True,
        )


class AskUserSubmitButton(discord.ui.Button["DiscordAskUserView"]):
    """Submit button for select-based prompts."""

    def __init__(self) -> None:
        super().__init__(label="Submit", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: Any) -> None:
        assert self.view is not None
        await self.view.submit_selected_answers(interaction)


class AskUserTextButton(discord.ui.Button["DiscordAskUserView"]):
    """Launch a modal for free-text answers."""

    def __init__(self) -> None:
        super().__init__(
            label="Answer",
            style=discord.ButtonStyle.primary,
            custom_id=_TEXT_MODAL_CUSTOM_ID,
        )

    async def callback(self, interaction: Any) -> None:
        assert self.view is not None
        if not self.view._interaction_allowed(interaction):
            await interaction.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(AskUserTextModal(self.view))


class AskUserTextModal(discord.ui.Modal):
    """Modal prompt for free-text ask_user answers."""

    def __init__(self, view: DiscordAskUserView) -> None:
        question = view._questions[0]
        raw_header = question.get("header")
        header = raw_header if isinstance(raw_header, str) else "Answer question"
        super().__init__(title=header[:45], custom_id=_TEXT_MODAL_CUSTOM_ID)
        self._view = view
        raw_prompt = question.get("question", "")
        prompt = raw_prompt if isinstance(raw_prompt, str) else str(raw_prompt)
        self.answer_input: Any = discord.ui.TextInput(
            label=(prompt[:45] or "Answer"),
            custom_id=f"{_TEXT_MODAL_CUSTOM_ID}:input",
            placeholder="Type your answer...",
            required=True,
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.add_item(self.answer_input)

    async def on_submit(self, interaction: Any) -> None:
        await self._view.finalize_answer(interaction, self.answer_input.value or "")


class DiscordAskUserView(discord.ui.View):
    """Transient Discord view for a single-question ask_user prompt."""

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

        question = questions[0]
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
            if len(options) <= (_MAX_BUTTONS_TOTAL - 1):
                self.add_item(AskUserTextButton())

    def bind_message_id(self, message_id: str) -> None:
        self._message_id = message_id

    def _question_prompt(self) -> str:
        prompt = self._questions[0].get("question", "")
        return prompt if isinstance(prompt, str) else str(prompt)

    def _current_selected_answers(self) -> list[str]:
        for child in self.children:
            if isinstance(child, AskUserSelect):
                return list(child.values)
        return list(self._selected_answers)

    async def submit_selected_answers(self, interaction: Any) -> None:
        selected_answers = self._current_selected_answers()
        if not selected_answers:
            await interaction.response.send_message(
                "Choose at least one option before submitting.",
                ephemeral=True,
            )
            return
        await self.finalize_answer(interaction, selected_answers)

    async def finalize_answer(self, interaction: Any, answer_value: str | list[str]) -> None:
        if not self._interaction_allowed(interaction):
            await interaction.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return

        if self._completed:
            await interaction.response.send_message(
                "This prompt has already been answered.",
                ephemeral=True,
            )
            return

        self._completed = True
        answer = {
            "answer": answer_value,
            "answered_by": str(interaction.user.id),
            "channel_id": str(interaction.channel.id) if interaction.channel is not None else "",
            "message_id": f"discord-{self._message_id}" if self._message_id else None,
        }
        if self._channel.on_ask_user_answer is not None:
            self._channel.on_ask_user_answer(self._request_id, answer)

        prompt = self._question_prompt()
        answer_text = ", ".join(answer_value) if isinstance(answer_value, list) else answer_value
        await interaction.response.edit_message(
            content=f"**Question:** {prompt}\n**Answer:** {answer_text}",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._forget()
        self.stop()

    async def on_timeout(self) -> None:
        if self._message_id is not None:
            for item in self.children:
                item.disabled = True
            try:
                channel = await self._channel._resolve_channel(self._jid)
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
            self._channel._ask_user_views.pop(self._message_id, None)

    def _interaction_allowed(self, interaction: Any) -> bool:
        user = interaction.user
        channel = interaction.channel
        guild = interaction.guild
        parent = getattr(channel, "parent", None)
        role_ids = frozenset(str(role.id) for role in getattr(user, "roles", []))
        parent_id = getattr(channel, "parent_id", None)
        author_names = frozenset(
            value
            for value in (
                getattr(user, "display_name", None),
                getattr(user, "global_name", None),
                getattr(user, "name", None),
                str(user),
            )
            if isinstance(value, str) and value.strip()
        )
        ctx = InboundContext(
            is_dm=guild is None,
            author_id=str(user.id),
            author_is_bot=bool(getattr(user, "bot", False)),
            guild_id=None if guild is None else str(guild.id),
            guild_name=None if guild is None else getattr(guild, "name", None),
            channel_id=str(channel.id) if channel is not None else "",
            channel_name=getattr(channel, "name", None),
            parent_channel_id=str(parent_id) if parent_id else None,
            parent_channel_name=getattr(parent, "name", None) if parent is not None else None,
            author_role_ids=role_ids,
            # Clicking a bot-owned component is the interaction equivalent of
            # explicitly addressing the bot, so mention-gated guilds should
            # treat it as intentional.
            mentions_bot=True,
            author_names=author_names,
        )
        return self._channel.access.decide(ctx) == "allow"
