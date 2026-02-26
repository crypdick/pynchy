"""Slack Block Kit builders and text utilities.

Standalone helpers with no dependency on SlackChannel state.
"""

from __future__ import annotations

import re
from typing import Any

# action_id prefixes used to match interaction callbacks:
#   ask_user_btn_{request_id}_{q_idx}_{label}  — option button click
#   ask_user_submit_{request_id}                — free-text submit button
#   ask_user_text_{request_id}_{q_idx}          — plain_text_input element
# Only btn (button click) and submit (text-input submit button) fire
# block_actions events. The input element uses dispatch_action=False so
# its action_id (ask_user_text_*) is never dispatched by Slack.
ASK_USER_ACTION_RE = re.compile(r"^ask_user_(btn|submit)_")


def normalize_chat_name(name: str) -> str:
    """Normalize Slack channel name to the canonical slug form."""
    cleaned = name.strip()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    return cleaned.lower().replace(" ", "-")


def split_text(text: str, *, max_len: int = 3000) -> list[str]:
    """Split text into chunks respecting the Slack block size limit.

    Tries to break on newlines when possible.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try to find a newline break point
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def build_ask_user_blocks(request_id: str, questions: list[dict]) -> list[dict]:
    """Build Block Kit blocks for an ask_user widget.

    Each question gets:
    - A ``section`` block with the question text (mrkdwn)
    - An ``actions`` block with buttons (one per option), if options exist
    - A ``divider`` between questions
    After all questions, a single ``input`` block with ``plain_text_input``
    and a submit button for free-form answers.
    """
    blocks: list[dict] = []

    for q_idx, q in enumerate(questions):
        question_text = q.get("question", "")
        options = q.get("options", [])

        # Section with question text
        blocks.append(
            {
                "type": "section",
                "block_id": f"ask_user_q_{request_id}_{q_idx}",
                "text": {"type": "mrkdwn", "text": f"*{question_text}*"},
            }
        )

        # Option buttons (if any)
        if options:
            buttons = []
            for opt in options:
                label = opt.get("label", "")
                desc = opt.get("description", "")
                button: dict[str, Any] = {
                    "type": "button",
                    "text": {"type": "plain_text", "text": label[:75]},
                    "action_id": f"ask_user_btn_{request_id}_{q_idx}_{label}",
                    "value": label,
                }
                if desc:
                    button["confirm"] = {
                        "title": {"type": "plain_text", "text": label},
                        "text": {"type": "mrkdwn", "text": desc},
                        "confirm": {"type": "plain_text", "text": "Select"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    }
                buttons.append(button)
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"ask_user_actions_{request_id}_{q_idx}",
                    "elements": buttons,
                }
            )

        # Divider between questions
        if q_idx < len(questions) - 1:
            blocks.append({"type": "divider"})

    # Free-form text input (always present — users can type a custom answer)
    blocks.append(
        {
            "type": "input",
            "block_id": f"ask_user_input_{request_id}_0",
            "optional": True,
            "dispatch_action": False,
            "element": {
                "type": "plain_text_input",
                "action_id": f"ask_user_text_{request_id}_0",
                "placeholder": {"type": "plain_text", "text": "Type a custom answer..."},
            },
            "label": {"type": "plain_text", "text": "Or type your answer"},
        }
    )

    # Submit button for the text input
    blocks.append(
        {
            "type": "actions",
            "block_id": f"ask_user_submit_actions_{request_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Submit"},
                    "action_id": f"ask_user_submit_{request_id}",
                    "value": "submit",
                    "style": "primary",
                }
            ],
        }
    )

    return blocks


def extract_text_input_value(body: dict, request_id: str) -> str:
    """Extract the plain_text_input value from a block_actions ``state.values``.

    Slack nests input values under ``state.values.<block_id>.<action_id>.value``.
    We search for the ask_user text input block matching ``request_id``.
    """
    values = body.get("state", {}).get("values", {})
    # Look for any block matching ask_user_input_{request_id}_*
    for block_id, actions in values.items():
        if not block_id.startswith(f"ask_user_input_{request_id}"):
            continue
        for action_id, payload in actions.items():
            if action_id.startswith(f"ask_user_text_{request_id}"):
                return payload.get("value", "") or ""
    return ""
