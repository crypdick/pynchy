"""Discord ask_user file-upload controls."""

from __future__ import annotations

from typing import Any, Protocol, cast, runtime_checkable

import discord

_FILE_MODAL_CUSTOM_ID = "au:file"
_MAX_FILE_UPLOADS = 10
_ASK_USER_VIEW_MISSING = "Discord ask_user callback called before the view was attached"


@runtime_checkable
class _AskUserView(Protocol):
    def primary_question(self) -> dict[str, Any]: ...

    def is_interaction_allowed(self, interaction: object) -> bool: ...

    async def finalize_answer(self, interaction: object, answer_value: object) -> None: ...


def _file_upload_settings(question: dict[str, Any]) -> tuple[bool, int] | None:
    """Return required/max-file settings for a Discord file-upload question."""
    raw_settings = question.get("fileUpload")
    if raw_settings is None or raw_settings is False:
        return None
    if isinstance(raw_settings, dict):
        required = bool(raw_settings.get("required", True))
        raw_max_files = raw_settings.get("maxFiles", 1)
        max_files = raw_max_files if isinstance(raw_max_files, int) else 1
    else:
        required = True
        max_files = 1
    return required, max(1, min(max_files, _MAX_FILE_UPLOADS))


def _attachment_metadata(attachment: object) -> dict[str, object]:
    """Convert a resolved Discord modal attachment into JSON-safe metadata."""
    value = cast("Any", attachment)
    size = getattr(value, "size", 0)
    return {
        "id": str(getattr(value, "id", "")),
        "filename": str(getattr(value, "filename", "")),
        "url": str(getattr(value, "url", "")),
        "proxy_url": str(getattr(value, "proxy_url", "")),
        "content_type": getattr(value, "content_type", None),
        "size": size if isinstance(size, int) else 0,
        "description": getattr(value, "description", None),
        "spoiler": bool(getattr(value, "spoiler", False)),
        "ephemeral": bool(getattr(value, "ephemeral", True)),
    }


def _require_ask_user_view(view: object) -> object:
    if view is None:
        raise RuntimeError(_ASK_USER_VIEW_MISSING)
    return view


class AskUserFileButton(discord.ui.Button[discord.ui.View]):
    """Launch a modal for file-upload answers."""

    def __init__(self) -> None:
        super().__init__(
            label="Attach files",
            style=discord.ButtonStyle.primary,
            custom_id=_FILE_MODAL_CUSTOM_ID,
        )

    async def callback(self, interaction: object) -> None:
        interaction_api = cast("Any", interaction)
        view = cast("_AskUserView", _require_ask_user_view(self.view))
        if not view.is_interaction_allowed(interaction_api):
            await interaction_api.response.send_message(
                "You are not allowed to answer this prompt.",
                ephemeral=True,
            )
            return
        await interaction_api.response.send_modal(AskUserFileModal(view))


class AskUserFileModal(discord.ui.Modal):
    """Modal prompt for file-upload ask_user answers."""

    def __init__(self, view: _AskUserView) -> None:
        question = view.primary_question()
        required, max_files = _file_upload_settings(question) or (True, 1)
        raw_header = question.get("header")
        header = raw_header if isinstance(raw_header, str) else "Upload files"
        super().__init__(title=header[:45], custom_id=_FILE_MODAL_CUSTOM_ID)
        self._view = view
        raw_prompt = question.get("question", "Upload files")
        prompt = raw_prompt if isinstance(raw_prompt, str) else str(raw_prompt)
        self.file_input: discord.ui.FileUpload[Any] = discord.ui.FileUpload(
            custom_id=f"{_FILE_MODAL_CUSTOM_ID}:input",
            required=required,
            min_values=1 if required else 0,
            max_values=max_files,
        )
        self.add_item(
            discord.ui.Label(text=(prompt[:45] or "Upload files"), component=self.file_input)
        )

    async def on_submit(self, interaction: object) -> None:  # noqa: V105
        answer = {"attachments": [_attachment_metadata(item) for item in self.file_input.values]}
        await self._view.finalize_answer(cast("Any", interaction), answer)
