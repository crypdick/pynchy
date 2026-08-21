"""Tests for warm-session IPC message formatting."""

from __future__ import annotations

from pynchy.host.orchestrator.ipc_message_formatting import format_messages_for_ipc


def test_formats_notices_and_escapes_message_xml() -> None:
    result = format_messages_for_ipc(
        [
            {
                "sender_name": "A&B",
                "timestamp": "2026-07-14T00:00:00Z",
                "content": 'Use <tags> & quotes "carefully"',
            }
        ],
        ["A host notice"],
    )

    assert result == (
        "<system_notices>\n- A host notice\n</system_notices>\n"
        "<messages>\n"
        '<message sender="A&amp;B" time="2026-07-14T00:00:00Z">'
        "Use &lt;tags&gt; &amp; quotes &quot;carefully&quot;</message>\n"
        "</messages>"
    )


def test_formats_notices_without_messages() -> None:
    assert format_messages_for_ipc([], ["A host notice"]) == (
        "<system_notices>\n- A host notice\n</system_notices>"
    )


def test_includes_semantic_context_without_metadata() -> None:
    result = format_messages_for_ipc(
        [
            {
                "sender_name": "Alice",
                "timestamp": "t1",
                "content": "See attachment",
                "context": {"attachments": [{"filename": "brief.pdf"}]},
                "metadata": {"source": "discord_canary", "synthetic_user_input": True},
            }
        ]
    )

    assert "<context>" in result
    assert "<metadata>" not in result
    assert "brief.pdf" in result
