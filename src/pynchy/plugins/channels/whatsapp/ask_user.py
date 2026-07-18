"""Public text-answer parsing for WhatsApp's ask_user choices."""

from __future__ import annotations

import re
from typing import Any


def resolve_ask_user_answer(content: str, questions: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve a WhatsApp text reply against the first question's options.

    WhatsApp presents ``ask_user`` choices as numbered text. Multi-question
    prompts preserve the reply as free-form text because a bare number cannot
    identify which question it answers.
    """
    content = content.strip()
    if questions:
        options = questions[0].get("options", [])
        # ``str.isdigit`` accepts superscript digits that ``int`` rejects;
        # only ASCII digits are valid numbered replies.
        if re.fullmatch(r"[0-9]+", content):
            index = int(content) - 1
            if 0 <= index < len(options):
                option = options[index]
                label = option.get("label", option) if isinstance(option, dict) else str(option)
                return {"answer": label}
    return {"answer": content}
