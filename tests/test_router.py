"""Tests for message formatting and outbound routing.

format_tool_preview has complex branching (one branch per tool type) and is
critical for UX — it determines what users see on WhatsApp/Telegram while the
agent works. format_messages_for_sdk controls what the LLM sees; wrong filtering
means host messages leak into prompts or user messages get dropped.
"""

from __future__ import annotations

from pynchy.messaging.router import (
    format_messages_for_sdk,
    format_tool_preview,
    parse_host_tag,
    strip_internal_tags,
)
from pynchy.types import NewMessage

# ---------------------------------------------------------------------------
# format_tool_preview — one branch per tool type
# ---------------------------------------------------------------------------


class TestFormatToolPreview:
    """Test tool preview formatting for channel messages.

    Each tool type extracts a different key from tool_input. Edge cases
    include missing keys, empty strings, and very long values.
    """

    # --- Bash ---
    def test_bash_shows_command(self):
        result = format_tool_preview("Bash", {"command": "git status"})
        assert result == "Bash: git status"

    def test_bash_truncates_long_command(self):
        long_cmd = "a" * 200
        result = format_tool_preview("Bash", {"command": long_cmd})
        assert len(result) <= 200
        assert result.endswith("...")

    def test_bash_empty_command(self):
        result = format_tool_preview("Bash", {"command": ""})
        assert result == "Bash"

    def test_bash_missing_command_key(self):
        result = format_tool_preview("Bash", {})
        assert result == "Bash"

    # --- Read / Edit / Write ---
    def test_read_shows_path(self):
        result = format_tool_preview("Read", {"file_path": "/src/main.py"})
        assert result == "Read: /src/main.py"

    def test_edit_shows_path(self):
        result = format_tool_preview("Edit", {"file_path": "/src/config.py"})
        assert result == "Edit: /src/config.py"

    def test_write_shows_path(self):
        result = format_tool_preview("Write", {"file_path": "/tmp/out.txt"})
        assert result == "Write: /tmp/out.txt"

    def test_read_truncates_long_path(self):
        long_path = "/very/long/" + "a" * 200
        result = format_tool_preview("Read", {"file_path": long_path})
        assert len(result) <= 200
        assert result.startswith("Read: ...")

    def test_read_missing_path(self):
        result = format_tool_preview("Read", {})
        assert result == "Read"

    # --- Grep ---
    def test_grep_shows_pattern(self):
        result = format_tool_preview("Grep", {"pattern": "TODO"})
        assert result == "Grep /TODO/"

    def test_grep_with_path(self):
        result = format_tool_preview("Grep", {"pattern": "TODO", "path": "src/"})
        assert result == "Grep /TODO/ src/"

    def test_grep_no_pattern(self):
        result = format_tool_preview("Grep", {})
        assert result == "Grep"

    # --- Glob ---
    def test_glob_shows_pattern(self):
        result = format_tool_preview("Glob", {"pattern": "**/*.py"})
        assert result == "Glob: **/*.py"

    def test_glob_no_pattern(self):
        result = format_tool_preview("Glob", {})
        assert result == "Glob"

    # --- WebFetch ---
    def test_webfetch_shows_url(self):
        result = format_tool_preview("WebFetch", {"url": "https://example.com"})
        assert result == "WebFetch: https://example.com"

    def test_webfetch_truncates_long_url(self):
        long_url = "https://example.com/" + "a" * 200
        result = format_tool_preview("WebFetch", {"url": long_url})
        assert len(result) <= 200
        assert result.endswith("...")

    def test_webfetch_no_url(self):
        result = format_tool_preview("WebFetch", {})
        assert result == "WebFetch"

    # --- WebSearch ---
    def test_websearch_shows_query(self):
        result = format_tool_preview("WebSearch", {"query": "python asyncio"})
        assert result == "WebSearch: python asyncio"

    def test_websearch_truncates_long_query(self):
        long_query = "q" * 200
        result = format_tool_preview("WebSearch", {"query": long_query})
        assert len(result) <= 200
        assert result.endswith("...")

    def test_websearch_no_query(self):
        result = format_tool_preview("WebSearch", {})
        assert result == "WebSearch"

    # --- Task ---
    def test_task_shows_description(self):
        result = format_tool_preview("Task", {"description": "search codebase"})
        assert result == "Task: search codebase"

    def test_task_no_description(self):
        result = format_tool_preview("Task", {})
        assert result == "Task"

    # --- AskUserQuestion ---
    def test_ask_user_question_shows_full_text(self):
        result = format_tool_preview(
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "question": "Which database should we use for the new feature?",
                        "header": "Database",
                        "options": [
                            {"label": "PostgreSQL", "description": "Relational DB"},
                            {"label": "MongoDB", "description": "Document DB"},
                        ],
                        "multiSelect": False,
                    }
                ]
            },
        )
        assert result == "Asking: Which database should we use for the new feature?"

    def test_ask_user_question_long_text_not_truncated(self):
        long_question = "Should we " + "refactor " * 50 + "this module?"
        result = format_tool_preview(
            "AskUserQuestion",
            {
                "questions": [
                    {"question": long_question, "header": "Q", "options": [], "multiSelect": False}
                ]
            },
        )
        assert long_question in result
        assert "..." not in result

    def test_ask_user_question_multiple_questions(self):
        result = format_tool_preview(
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "question": "First question?",
                        "header": "Q1",
                        "options": [],
                        "multiSelect": False,
                    },
                    {
                        "question": "Second question?",
                        "header": "Q2",
                        "options": [],
                        "multiSelect": False,
                    },
                ]
            },
        )
        assert result == "Asking: First question? | Second question?"

    def test_ask_user_question_empty_questions(self):
        result = format_tool_preview("AskUserQuestion", {"questions": []})
        assert result == "AskUserQuestion"

    def test_ask_user_question_no_questions_key(self):
        result = format_tool_preview("AskUserQuestion", {})
        assert result == "AskUserQuestion"

    # --- Fallback (unknown tools) ---
    def test_unknown_tool_shows_input(self):
        result = format_tool_preview("CustomTool", {"key": "value"})
        assert result.startswith("CustomTool: ")
        assert "key" in result

    def test_unknown_tool_truncates_large_input(self):
        big_input = {f"key{i}": "x" * 50 for i in range(10)}
        result = format_tool_preview("CustomTool", big_input)
        assert len(result) <= 200

    def test_unknown_tool_empty_input(self):
        result = format_tool_preview("CustomTool", {})
        assert result == "CustomTool"


# ---------------------------------------------------------------------------
# format_messages_for_sdk — LLM input filtering
# ---------------------------------------------------------------------------


def _make_message(
    *,
    content: str = "hello",
    message_type: str = "user",
    sender: str = "user123",
    sender_name: str = "User",
) -> NewMessage:
    return NewMessage(
        id="msg-1",
        chat_jid="test@g.us",
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp="2026-01-01T00:00:00Z",
        message_type=message_type,
    )


class TestFormatMessagesForSdk:
    """Test message filtering for the LLM conversation.

    Host messages are operational notifications that must NEVER be sent to
    the LLM. If they leak through, the agent may try to "respond" to system
    status messages.
    """

    def test_user_messages_pass_through(self):
        msgs = [_make_message(content="hello", message_type="user")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["content"] == "hello"
        assert result[0]["message_type"] == "user"

    def test_assistant_messages_pass_through(self):
        msgs = [_make_message(content="reply", message_type="assistant")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["message_type"] == "assistant"

    def test_system_messages_pass_through(self):
        msgs = [_make_message(content="context", message_type="system")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["message_type"] == "system"

    def test_host_messages_are_filtered_out(self):
        """Host messages are operational and must not reach the LLM."""
        msgs = [
            _make_message(content="hello", message_type="user"),
            _make_message(content="⚠️ Agent error", message_type="host"),
            _make_message(content="follow up", message_type="user"),
        ]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 2
        assert all(m["message_type"] != "host" for m in result)

    def test_tool_result_messages_pass_through(self):
        msgs = [_make_message(content="output", message_type="tool_result")]
        result = format_messages_for_sdk(msgs)
        assert len(result) == 1
        assert result[0]["message_type"] == "tool_result"

    def test_preserves_message_order(self):
        msgs = [
            _make_message(content="first", message_type="user"),
            _make_message(content="second", message_type="assistant"),
            _make_message(content="third", message_type="user"),
        ]
        result = format_messages_for_sdk(msgs)
        assert [m["content"] for m in result] == ["first", "second", "third"]

    def test_empty_input_returns_empty(self):
        result = format_messages_for_sdk([])
        assert result == []

    def test_all_host_returns_empty(self):
        """If all messages are host messages, result should be empty."""
        msgs = [
            _make_message(content="status", message_type="host"),
            _make_message(content="error", message_type="host"),
        ]
        result = format_messages_for_sdk(msgs)
        assert result == []

    def test_preserves_metadata(self):
        msg = _make_message(content="test")
        msg.metadata = {"source": "whatsapp"}
        result = format_messages_for_sdk([msg])
        assert result[0]["metadata"] == {"source": "whatsapp"}

    def test_preserves_sender_info(self):
        msg = _make_message(sender="alice", sender_name="Alice")
        result = format_messages_for_sdk([msg])
        assert result[0]["sender"] == "alice"
        assert result[0]["sender_name"] == "Alice"


# ---------------------------------------------------------------------------
# strip_internal_tags
# ---------------------------------------------------------------------------


class TestStripInternalTags:
    """Test removal of <internal>...</internal> blocks."""

    def test_removes_single_internal_block(self):
        text = "Hello <internal>secret</internal> world"
        assert strip_internal_tags(text) == "Hello  world"

    def test_removes_multiple_internal_blocks(self):
        text = "<internal>a</internal> visible <internal>b</internal>"
        assert strip_internal_tags(text) == "visible"

    def test_preserves_text_without_tags(self):
        assert strip_internal_tags("plain text") == "plain text"

    def test_removes_multiline_internal_blocks(self):
        text = "before\n<internal>\nmultiline\ncontent\n</internal>\nafter"
        result = strip_internal_tags(text)
        assert "multiline" not in result
        assert "before" in result
        assert "after" in result

    def test_empty_string(self):
        assert strip_internal_tags("") == ""

    def test_only_internal_content(self):
        assert strip_internal_tags("<internal>everything</internal>") == ""


# ---------------------------------------------------------------------------
# parse_host_tag
# ---------------------------------------------------------------------------


class TestParseHostTag:
    """Test detection and extraction of <host> tagged messages."""

    def test_detects_host_tag(self):
        is_host, content = parse_host_tag("<host>System message</host>")
        assert is_host is True
        assert content == "System message"

    def test_non_host_message(self):
        is_host, content = parse_host_tag("Regular message")
        assert is_host is False
        assert content == "Regular message"

    def test_host_tag_with_whitespace(self):
        is_host, content = parse_host_tag("  <host> padded </host>  ")
        assert is_host is True
        assert content == "padded"

    def test_partial_host_tag_not_matched(self):
        """Host tag must wrap the entire text."""
        is_host, content = parse_host_tag("prefix <host>inner</host> suffix")
        assert is_host is False

    def test_empty_host_tag(self):
        is_host, content = parse_host_tag("<host></host>")
        assert is_host is True
        assert content == ""

    def test_multiline_host_content(self):
        text = "<host>line1\nline2</host>"
        is_host, content = parse_host_tag(text)
        assert is_host is True
        assert "line1" in content
        assert "line2" in content
