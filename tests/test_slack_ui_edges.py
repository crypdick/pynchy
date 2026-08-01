"""Slack Block Kit and interaction payload behavior."""

from __future__ import annotations

from pynchy.plugins.channels.slack.api import (
    build_ask_user_blocks,
    extract_checkbox_values,
    extract_text_input_value,
    split_text,
)


def test_split_text_emits_final_short_chunk() -> None:
    text = "x" * 3001

    assert split_text(text) == ["x" * 3000, "x"]


def test_split_text_drops_newline_only_tail_after_boundary() -> None:
    text = "x" * 2999 + "\n\n"

    assert split_text(text) == ["x" * 2999]


def test_ask_user_blocks_render_options_descriptions_and_question_divider() -> None:
    blocks = build_ask_user_blocks(
        "request-1",
        [
            {"question": "Pick one", "options": [{"label": "A", "description": "First"}]},
            {"question": "Anything else?"},
        ],
    )

    assert blocks[1]["elements"][0]["options"][0]["description"]["text"] == "First"
    assert blocks[2] == {"type": "divider"}
    assert blocks[3]["type"] == "section"


def test_text_input_extraction_ignores_other_blocks_and_empty_values() -> None:
    body = {
        "state": {
            "values": {
                "other-block": {"ask_user_text_request-1": {"value": "wrong"}},
                "ask_user_input_request-1_unused": {"other-action": {"value": "wrong"}},
                "ask_user_input_request-1_0": {
                    "other-action": {"value": "wrong"},
                    "ask_user_text_request-1_0": {"value": None},
                },
            }
        }
    }

    assert not extract_text_input_value(body, "request-1")


def test_checkbox_extraction_collects_values_and_ignores_empty_labels() -> None:
    body = {
        "state": {
            "values": {
                "other-block": {
                    "ask_user_checkbox_request-1_0": {"selected_options": [{"value": "wrong"}]}
                },
                "ask_user_actions_request-1_0": {
                    "ask_user_checkbox_request-1_0": {
                        "selected_options": [{"value": "A"}, {"value": ""}]
                    }
                },
                "ask_user_actions_request-1_1": {
                    "ask_user_checkbox_request-1_1": {"selected_options": [{"value": "B"}]}
                },
            }
        }
    }

    assert extract_checkbox_values(body, "request-1") == "A, B"
