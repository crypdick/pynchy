"""Tests for claude core transcript parsing helpers.

These functions are pure logic with no SDK dependency — they parse JSONL
transcripts and format them as markdown for archival.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Mock claude_agent_sdk so we can import container code on the host
sys.modules.setdefault("claude_agent_sdk", MagicMock())

sys.path.insert(0, str(Path(__file__).parent.parent / "container" / "agent_runner" / "src"))
from agent_runner.cores.claude import (  # noqa: E402
    _format_transcript_markdown,
    _generate_fallback_name,
    _get_session_summary,
    _parse_transcript,
    _sanitize_filename,
)

# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    """Test filename sanitization for conversation archives."""

    def test_basic_text(self):
        result = _sanitize_filename("my conversation")
        assert result == "my-conversation"

    def test_special_characters_replaced(self):
        result = _sanitize_filename("Fix bug #123: crash on startup!")
        assert result == "fix-bug-123-crash-on-startup"

    def test_leading_trailing_hyphens_stripped(self):
        result = _sanitize_filename("!!! hello !!!")
        assert result == "hello"

    def test_consecutive_special_chars_collapsed(self):
        result = _sanitize_filename("a...b---c   d")
        assert result == "a-b-c-d"

    def test_uppercase_lowered(self):
        result = _sanitize_filename("Hello World")
        assert result == "hello-world"

    def test_truncated_to_50_chars(self):
        long_input = "a" * 100
        result = _sanitize_filename(long_input)
        assert len(result) <= 50

    def test_empty_input(self):
        result = _sanitize_filename("")
        assert result == ""

    def test_only_special_chars(self):
        result = _sanitize_filename("!@#$%^&*()")
        assert result == ""

    def test_numbers_preserved(self):
        result = _sanitize_filename("version 2.0 release")
        assert result == "version-2-0-release"

    def test_unicode_removed(self):
        result = _sanitize_filename("café résumé")
        # Non-ascii chars are replaced by the regex
        assert "-" in result or result == "caf-r-sum"


# ---------------------------------------------------------------------------
# _generate_fallback_name
# ---------------------------------------------------------------------------


class TestGenerateFallbackName:
    """Test fallback name generation."""

    def test_format(self):
        name = _generate_fallback_name()
        assert name.startswith("conversation-")
        # Should have 4-digit time suffix (HHMM)
        time_part = name[len("conversation-") :]
        assert len(time_part) == 4
        assert time_part.isdigit()

    def test_hours_minutes_padded(self):
        name = _generate_fallback_name()
        time_part = name[len("conversation-") :]
        hours = int(time_part[:2])
        minutes = int(time_part[2:])
        assert 0 <= hours <= 23
        assert 0 <= minutes <= 59


# ---------------------------------------------------------------------------
# _parse_transcript
# ---------------------------------------------------------------------------


class TestParseTranscript:
    """Test JSONL transcript parsing."""

    def test_empty_content(self):
        assert _parse_transcript("") == []

    def test_blank_lines_skipped(self):
        assert _parse_transcript("\n\n\n") == []

    def test_single_user_message_string_content(self):
        entry = {"type": "user", "message": {"content": "Hello"}}
        result = _parse_transcript(json.dumps(entry))
        assert len(result) == 1
        assert result[0] == {"role": "user", "content": "Hello"}

    def test_single_user_message_list_content(self):
        """User message with content as list of blocks (multimodal format)."""
        entry = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world"},
                ]
            },
        }
        result = _parse_transcript(json.dumps(entry))
        assert len(result) == 1
        assert result[0]["content"] == "Hello world"

    def test_assistant_message(self):
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I can help with that."},
                ]
            },
        }
        result = _parse_transcript(json.dumps(entry))
        assert len(result) == 1
        assert result[0] == {"role": "assistant", "content": "I can help with that."}

    def test_assistant_non_text_blocks_filtered(self):
        """Tool use blocks should be filtered out, only text extracted."""
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "bash", "input": {}},
                    {"type": "text", "text": "Done!"},
                ]
            },
        }
        result = _parse_transcript(json.dumps(entry))
        assert len(result) == 1
        assert result[0]["content"] == "Done!"

    def test_multi_turn_conversation(self):
        lines = [
            json.dumps({"type": "user", "message": {"content": "What is 2+2?"}}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "4"}]},
                }
            ),
            json.dumps({"type": "user", "message": {"content": "Thanks!"}}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "You're welcome!"}]},
                }
            ),
        ]
        result = _parse_transcript("\n".join(lines))
        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "user"
        assert result[3]["role"] == "assistant"

    def test_malformed_json_skipped(self):
        content = "not valid json\n" + json.dumps({"type": "user", "message": {"content": "valid"}})
        result = _parse_transcript(content)
        assert len(result) == 1
        assert result[0]["content"] == "valid"

    def test_unknown_type_skipped(self):
        entry = {"type": "system", "message": {"content": "system msg"}}
        result = _parse_transcript(json.dumps(entry))
        assert result == []

    def test_empty_content_skipped(self):
        entry = {"type": "user", "message": {"content": ""}}
        result = _parse_transcript(json.dumps(entry))
        assert result == []

    def test_missing_message_key_skipped(self):
        entry = {"type": "user"}
        result = _parse_transcript(json.dumps(entry))
        assert result == []

    def test_assistant_only_tool_use_blocks_empty(self):
        """Assistant message with only tool use blocks produces no text."""
        entry = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "bash", "input": {"command": "ls"}},
                    {"type": "tool_result", "content": "file.txt"},
                ]
            },
        }
        result = _parse_transcript(json.dumps(entry))
        assert result == []

    def test_user_content_list_with_non_text_blocks(self):
        """User content as list with blocks missing 'text' key."""
        entry = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "image", "source": "data:..."},
                    {"type": "text", "text": "Describe this image"},
                ]
            },
        }
        result = _parse_transcript(json.dumps(entry))
        assert len(result) == 1
        assert result[0]["content"] == "Describe this image"


# ---------------------------------------------------------------------------
# _format_transcript_markdown
# ---------------------------------------------------------------------------


class TestFormatTranscriptMarkdown:
    """Test markdown formatting of parsed transcripts."""

    def test_empty_messages(self):
        result = _format_transcript_markdown([])
        assert "# Conversation" in result
        assert "Archived:" in result

    def test_with_title(self):
        result = _format_transcript_markdown([], title="Debug Session")
        assert "# Debug Session" in result

    def test_user_message_formatted(self):
        messages = [{"role": "user", "content": "Hello"}]
        result = _format_transcript_markdown(messages)
        assert "**User**: Hello" in result

    def test_assistant_message_formatted(self):
        messages = [{"role": "assistant", "content": "Hi there"}]
        result = _format_transcript_markdown(messages)
        assert "**Pynchy**: Hi there" in result

    def test_long_content_truncated(self):
        long_content = "x" * 3000
        messages = [{"role": "user", "content": long_content}]
        result = _format_transcript_markdown(messages)
        # Content should be truncated to 2000 chars + "..."
        assert "..." in result
        # The full 3000-char string should NOT appear
        assert long_content not in result

    def test_multiple_messages_ordered(self):
        messages = [
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1"},
            {"role": "user", "content": "Question 2"},
        ]
        result = _format_transcript_markdown(messages)
        # Check ordering
        idx_q1 = result.index("Question 1")
        idx_a1 = result.index("Answer 1")
        idx_q2 = result.index("Question 2")
        assert idx_q1 < idx_a1 < idx_q2

    def test_contains_separator(self):
        result = _format_transcript_markdown([])
        assert "---" in result

    def test_contains_archive_date(self):
        result = _format_transcript_markdown([])
        assert "Archived:" in result

    def test_content_exactly_2000_chars_not_truncated(self):
        """Content exactly at the limit should NOT be truncated."""
        content = "x" * 2000
        messages = [{"role": "user", "content": content}]
        result = _format_transcript_markdown(messages)
        assert "..." not in result
        assert content in result

    def test_content_2001_chars_truncated(self):
        """Content one char over the limit should be truncated."""
        content = "x" * 2001
        messages = [{"role": "user", "content": content}]
        result = _format_transcript_markdown(messages)
        assert "..." in result
        assert content not in result


# ---------------------------------------------------------------------------
# _get_session_summary
# ---------------------------------------------------------------------------


class TestGetSessionSummary:
    """Test session summary lookup from sessions-index.json."""

    def test_returns_summary_for_matching_session(self, tmp_path):
        index = {
            "entries": [
                {"sessionId": "sess-1", "summary": "Debugging auth flow"},
                {"sessionId": "sess-2", "summary": "Refactoring tests"},
            ]
        }
        index_path = tmp_path / "sessions-index.json"
        index_path.write_text(json.dumps(index))
        transcript_path = str(tmp_path / "transcript.jsonl")

        result = _get_session_summary("sess-1", transcript_path)
        assert result == "Debugging auth flow"

    def test_returns_none_for_nonexistent_session(self, tmp_path):
        index = {"entries": [{"sessionId": "other", "summary": "Other session"}]}
        (tmp_path / "sessions-index.json").write_text(json.dumps(index))
        transcript_path = str(tmp_path / "transcript.jsonl")

        result = _get_session_summary("not-found", transcript_path)
        assert result is None

    def test_returns_none_when_index_missing(self, tmp_path):
        transcript_path = str(tmp_path / "transcript.jsonl")
        result = _get_session_summary("sess-1", transcript_path)
        assert result is None

    def test_returns_none_for_empty_entries(self, tmp_path):
        (tmp_path / "sessions-index.json").write_text(json.dumps({"entries": []}))
        transcript_path = str(tmp_path / "transcript.jsonl")

        result = _get_session_summary("sess-1", transcript_path)
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        (tmp_path / "sessions-index.json").write_text("not valid json")
        transcript_path = str(tmp_path / "transcript.jsonl")

        result = _get_session_summary("sess-1", transcript_path)
        assert result is None

    def test_uses_transcript_parent_dir_for_index(self, tmp_path):
        """Index is looked up in the parent directory of the transcript."""
        subdir = tmp_path / "project" / ".claude"
        subdir.mkdir(parents=True)
        index = {"entries": [{"sessionId": "s1", "summary": "Found it"}]}
        (subdir / "sessions-index.json").write_text(json.dumps(index))
        transcript_path = str(subdir / "transcript.jsonl")

        result = _get_session_summary("s1", transcript_path)
        assert result == "Found it"
