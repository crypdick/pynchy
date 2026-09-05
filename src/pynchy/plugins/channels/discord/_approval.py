"""Discord controls for security approval prompts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import discord

from pynchy.workspace.api import APPROVAL_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from ._channel import DiscordChannel
else:
    DiscordChannel = object

_VIEW_MISSING = "Discord approval callback called before the view was attached"


def _require_view(view: discord.ui.View | None) -> DiscordApprovalView:
    if view is None:
        raise RuntimeError(_VIEW_MISSING)
    return cast("DiscordApprovalView", view)


class ApprovalButton(discord.ui.Button["DiscordApprovalView"]):
    """One irreversible approval decision."""

    def __init__(self, *, action: str, label: str, style: discord.ButtonStyle) -> None:
        super().__init__(label=label, custom_id=f"approval:{action}", style=style)
        self._action = action

    async def callback(self, interaction: object) -> None:
        await _require_view(self.view).finalize(cast("Any", interaction), self._action)


class DiscordApprovalView(discord.ui.View):
    """Time-bound Approve/Deny controls for one Pynchy approval request."""

    def __init__(
        self,
        *,
        channel: DiscordChannel,
        jid: str,
        short_id: str,
        content: str,
        allow_remember: bool = False,
    ) -> None:
        super().__init__(timeout=APPROVAL_TIMEOUT_SECONDS)
        self._channel = channel
        self._jid = jid
        self._short_id = short_id
        self._content = content
        self._message_id: str | None = None
        self._completed = False
        if allow_remember:
            for action, label in (
                ("approve-once", "Approve once"),
                ("approve-session", "Approve this session"),
                ("approve-forever", "Approve forever"),
            ):
                self.add_item(
                    ApprovalButton(action=action, label=label, style=discord.ButtonStyle.primary)
                )
        else:
            self.add_item(
                ApprovalButton(action="approve", label="Approve", style=discord.ButtonStyle.primary)
            )
        self.add_item(ApprovalButton(action="deny", label="Deny", style=discord.ButtonStyle.danger))

    def bind_message_id(self, message_id: str) -> None:
        self._message_id = message_id

    async def finalize(self, interaction: object, action: str) -> None:
        interaction_api = cast("Any", interaction)
        if not self._channel.is_interaction_allowed(interaction_api):
            await interaction_api.response.send_message(
                "You are not allowed to decide this approval.", ephemeral=True
            )
            return
        if self._completed:
            await interaction_api.response.send_message(
                "This approval has already been decided.", ephemeral=True
            )
            return
        if self._channel.on_approval_decision is None:
            await interaction_api.response.send_message(
                "Approval controls are unavailable; use the command in the prompt instead.",
                ephemeral=True,
            )
            return

        self._completed = True
        self._channel.on_approval_decision(
            self._jid, action, self._short_id, str(interaction_api.user.id)
        )

        verb = "Approved" if action.startswith("approve") else "Denied"
        await interaction_api.response.edit_message(
            content=f"{self._content}\n\n\u2705 {verb}",
            view=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.stop()

    async def on_timeout(self) -> None:  # noqa: V105
        if self._message_id is None or self._completed:
            return
        for item in self.children:
            item.disabled = True  # noqa: V101
        try:
            channel = await self._channel.resolve_channel(self._jid)
            message = await channel.fetch_message(int(self._message_id))
            await message.edit(
                content=f"{self._content}\n\n_This approval expired._",
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException:
            pass
