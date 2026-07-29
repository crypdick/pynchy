"""Public config reference parsing behavior."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pynchy.config.api import parse_chat_ref, validate_settings_mapping


def test_chat_ref_parser_preserves_dotted_channel_id() -> None:
    parsed = parse_chat_ref("connection.slack.primary.chat.C123.thread.456")

    assert parsed is not None
    assert (parsed.platform, parsed.name, parsed.chat) == ("slack", "primary", "C123.thread.456")


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "connection.slack",
        "channel.slack.primary.chat.C123",
        "connection.slack.primary.channel.C123",
        "connection..primary.chat.C123",
        "connection.slack..chat.C123",
        "connection.slack.primary.chat.",
    ],
)
def test_workspace_chat_rejects_malformed_chat_reference(reference: str) -> None:
    with pytest.raises(ValidationError, match="chat must be connection"):
        validate_settings_mapping({"workspaces": {"team": {"chat": reference}}})
