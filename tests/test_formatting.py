"""Tests for trigger pattern, outbound formatting, and tool previews."""

from __future__ import annotations

from dataclasses import dataclass

from conftest import make_settings

from pynchy.host.orchestrator.messaging.formatter import (
    format_outbound,
    format_tool_preview,
    parse_host_tag,
    strip_internal_tags,
)
from pynchy.types import NewMessage

s = make_settings()
TRIGGER_PATTERN = s.trigger_pattern

# --- TRIGGER_PATTERN ---


class TestTriggerPattern:
    def test_matches_at_name_at_start(self):
        assert TRIGGER_PATTERN.search("@pynchy hello")

    def test_matches_case_insensitively(self):
        assert TRIGGER_PATTERN.search("@Pynchy hello")
        assert TRIGGER_PATTERN.search("@PYNCHY hello")

    def test_does_not_match_when_not_at_start(self):
        assert not TRIGGER_PATTERN.search("hello @pynchy")

    def test_does_not_match_partial_name(self):
        assert not TRIGGER_PATTERN.search("@pynchybot hello")

    def test_matches_with_word_boundary_before_apostrophe(self):
        assert TRIGGER_PATTERN.search("@pynchy's thing")

    def test_matches_name_alone(self):
        assert TRIGGER_PATTERN.search("@pynchy")

    def test_matches_with_leading_whitespace_after_trim(self):
        assert TRIGGER_PATTERN.search("@pynchy hey".strip())

    def test_matches_ghost_alias(self):
        assert TRIGGER_PATTERN.search("@ghost hello")

    def test_matches_ghost_alias_case_insensitively(self):
        assert TRIGGER_PATTERN.search("@Ghost hello")
        assert TRIGGER_PATTERN.search("@GHOST hello")

    def test_ghost_alias_does_not_match_partial(self):
        assert not TRIGGER_PATTERN.search("@ghostly hello")

    def test_ghost_alias_does_not_match_when_not_at_start(self):
        assert not TRIGGER_PATTERN.search("hello @ghost")


# --- stripInternalTags ---


class TestStripInternalTags:
    def test_strips_single_line_internal_tags(self):
        assert strip_internal_tags("hello <internal>secret</internal> world") == "hello  world"

    def test_strips_multi_line_internal_tags(self):
        assert (
            strip_internal_tags("hello <internal>\nsecret\nstuff\n</internal> world")
            == "hello  world"
        )

    def test_strips_multiple_internal_tag_blocks(self):
        assert strip_internal_tags("<internal>a</internal>hello<internal>b</internal>") == "hello"

    def test_returns_empty_when_only_internal_tags(self):
        assert strip_internal_tags("<internal>only this</internal>") == ""


# --- formatOutbound ---


class TestFormatOutbound:
    @dataclass
    class _FakeChannel:
        """Minimal channel stub for testing prefix behavior."""

        prefix_assistant_name: object = True  # object to allow None/missing

    def test_prefixes_with_lobster_emoji(self):
        ch = self._FakeChannel(prefix_assistant_name=True)
        assert format_outbound(ch, "hello world") == "🦞 hello world"

    def test_does_not_prefix_when_opted_out(self):
        ch = self._FakeChannel(prefix_assistant_name=False)
        assert format_outbound(ch, "hello world") == "hello world"

    def test_defaults_to_prefixing_when_undefined(self):
        ch = self._FakeChannel(prefix_assistant_name=None)
        assert format_outbound(ch, "hello world") == "🦞 hello world"

    def test_returns_empty_when_all_internal(self):
        ch = self._FakeChannel(prefix_assistant_name=True)
        assert format_outbound(ch, "<internal>hidden</internal>") == ""

    def test_strips_internal_and_prefixes_remaining(self):
        ch = self._FakeChannel(prefix_assistant_name=True)
        result = format_outbound(ch, "<internal>thinking</internal>The answer is 42")
        assert result == "🦞 The answer is 42"


# --- Trigger gating with trigger mode ---


class TestTriggerGating:
    """Replicates the trigger gating logic from the orchestrator.

    The resolved trigger field is Literal["mention", "always"]:
    - "mention": require @trigger pattern in messages (unless admin group)
    - "always": always process messages
    Admin groups always process regardless of trigger mode.
    """

    @staticmethod
    def _should_process(
        is_admin_group: bool,
        trigger: str,
        messages: list[NewMessage],
    ) -> bool:
        if is_admin_group:
            return True
        if trigger == "always":
            return True
        # trigger == "mention"
        return any(TRIGGER_PATTERN.search(m.content.strip()) for m in messages)

    def test_admin_group_always_processes(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(True, "mention", msgs)

    def test_non_admin_mention_requires_trigger(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert not self._should_process(False, "mention", msgs)

    def test_non_admin_mention_processes_when_trigger_present(self, make_msg):
        msgs = [make_msg(content="@pynchy do something")]
        assert self._should_process(False, "mention", msgs)

    def test_non_admin_always_processes_without_trigger(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(False, "always", msgs)


# --- parseHostTag ---


class TestParseHostTag:
    """Test the parse_host_tag function which extracts host-tagged content.

    Host tags (<host>...</host>) are used to indicate messages from the system
    that should be formatted differently in the UI (not prefixed with assistant name).
    """

    def test_recognizes_host_tag(self):
        is_host, content = parse_host_tag("<host>System message</host>")
        assert is_host is True
        assert content == "System message"

    def test_extracts_multiline_content(self):
        is_host, content = parse_host_tag("<host>Line 1\nLine 2\nLine 3</host>")
        assert is_host is True
        assert content == "Line 1\nLine 2\nLine 3"

    def test_trims_whitespace_inside_tags(self):
        is_host, content = parse_host_tag("<host>  \n  message  \n  </host>")
        assert is_host is True
        assert content == "message"

    def test_requires_tags_at_start_and_end(self):
        # Tags must wrap the entire string
        is_host, content = parse_host_tag("prefix <host>message</host>")
        assert is_host is False
        assert content == "prefix <host>message</host>"

    def test_requires_closing_tag(self):
        is_host, content = parse_host_tag("<host>message")
        assert is_host is False
        assert content == "<host>message"

    def test_returns_false_for_plain_text(self):
        is_host, content = parse_host_tag("Just a regular message")
        assert is_host is False
        assert content == "Just a regular message"

    def test_returns_false_for_empty_string(self):
        is_host, content = parse_host_tag("")
        assert is_host is False
        assert content == ""

    def test_handles_whitespace_around_tags(self):
        is_host, content = parse_host_tag("  <host>message</host>  ")
        assert is_host is True
        assert content == "message"

    def test_empty_host_tag(self):
        is_host, content = parse_host_tag("<host></host>")
        assert is_host is True
        assert content == ""

    def test_preserves_internal_xml(self):
        is_host, content = parse_host_tag("<host>Message with <b>bold</b> text</host>")
        assert is_host is True
        assert content == "Message with <b>bold</b> text"


# --- formatToolPreview ---


class TestFormatToolPreview:
    """Test the format_tool_preview function which creates human-readable
    summaries of tool invocations for display in chat interfaces.

    This function has complex branching logic and is critical for user experience -
    users need to understand what the agent is doing at a glance.
    """

    # Bash tool
    def test_bash_with_command(self):
        result = format_tool_preview("Bash", {"command": "ls -la"})
        assert result == "Bash:\n```\nls -la\n```"

    def test_bash_shows_full_long_command(self):
        long_cmd = "echo " + "x" * 200
        result = format_tool_preview("Bash", {"command": long_cmd})
        assert long_cmd in result
        assert "```" in result

    def test_bash_preserves_medium_commands(self):
        cmd = "echo " + "x" * 100
        result = format_tool_preview("Bash", {"command": cmd})
        assert cmd in result
        assert "```" in result

    def test_bash_without_command(self):
        result = format_tool_preview("Bash", {})
        assert result == "Bash"

    # Read/Edit/Write tools
    def test_read_with_path(self):
        result = format_tool_preview("Read", {"file_path": "/path/to/file.py"})
        assert result == "Read: /path/to/file.py"

    def test_edit_with_path(self):
        result = format_tool_preview("Edit", {"file_path": "/src/main.py"})
        assert result == "Edit: /src/main.py"

    def test_write_with_path(self):
        result = format_tool_preview("Write", {"file_path": "/tmp/output.txt"})
        assert result == "Write: /tmp/output.txt"

    def test_read_truncates_long_paths(self):
        long_path = "/very/long/" + "deep/" * 40 + "file.txt"
        result = format_tool_preview("Read", {"file_path": long_path})
        assert result.startswith("Read: ...")
        assert len(result) <= 159  # "Read: " (6) + "..." (3) + 147 chars

    def test_read_without_path(self):
        result = format_tool_preview("Read", {})
        assert result == "Read"

    # Grep tool
    def test_grep_with_pattern_only(self):
        result = format_tool_preview("Grep", {"pattern": "TODO"})
        assert result == "Grep /TODO/"

    def test_grep_with_pattern_and_path(self):
        result = format_tool_preview("Grep", {"pattern": "def main", "path": "src/"})
        assert result == "Grep /def main/ src/"

    def test_grep_without_pattern(self):
        result = format_tool_preview("Grep", {})
        assert result == "Grep"

    def test_grep_with_path_only(self):
        result = format_tool_preview("Grep", {"path": "src/"})
        assert result == "Grep src/"

    # Glob tool
    def test_glob_with_pattern(self):
        result = format_tool_preview("Glob", {"pattern": "**/*.py"})
        assert result == "Glob: **/*.py"

    def test_glob_without_pattern(self):
        result = format_tool_preview("Glob", {})
        assert result == "Glob"

    # Unknown tools - fallback behavior
    def test_unknown_tool_with_empty_input(self):
        result = format_tool_preview("UnknownTool", {})
        assert result == "UnknownTool"

    def test_unknown_tool_with_input(self):
        result = format_tool_preview("CustomTool", {"param": "value"})
        assert "CustomTool:" in result
        assert "param" in result

    def test_unknown_tool_truncates_long_input(self):
        long_input = {"data": "x" * 200}
        result = format_tool_preview("CustomTool", long_input)
        assert result.endswith("...")
        # Check that truncation happened
        assert len(result) < len(f"CustomTool: {long_input}")
        assert len(result) <= 163  # "CustomTool: " (12) + 147 chars + "..." (3)

    # Edge cases
    def test_empty_tool_name(self):
        result = format_tool_preview("", {"command": "test"})
        assert ": " in result  # Should still format somehow

    def test_none_values_in_input(self):
        result = format_tool_preview("Bash", {"command": None})
        # Should not crash, should handle gracefully
        assert "Bash" in result

    def test_special_characters_in_command(self):
        result = format_tool_preview("Bash", {"command": 'echo "hello & goodbye"'})
        assert 'echo "hello & goodbye"' in result
        assert "```" in result

    def test_multiline_command(self):
        result = format_tool_preview("Bash", {"command": "echo foo\necho bar"})
        assert "echo foo\necho bar" in result
        assert "```" in result

    # WebFetch tool
    def test_webfetch_with_url(self):
        result = format_tool_preview("WebFetch", {"url": "https://example.com"})
        assert result == "WebFetch: https://example.com"

    def test_webfetch_truncates_long_url(self):
        long_url = "https://example.com/very/long/" + "path/" * 40
        result = format_tool_preview("WebFetch", {"url": long_url})
        assert result.startswith("WebFetch: ")
        assert result.endswith("...")
        assert len(result) <= 163  # "WebFetch: " (10) + 147 chars + "..." (3)

    def test_webfetch_without_url(self):
        result = format_tool_preview("WebFetch", {})
        assert result == "WebFetch"

    # WebSearch tool
    def test_websearch_with_query(self):
        result = format_tool_preview("WebSearch", {"query": "python async tutorial"})
        assert result == "WebSearch: python async tutorial"

    def test_websearch_truncates_long_query(self):
        long_query = "how to " + "very " * 50 + "thing"
        result = format_tool_preview("WebSearch", {"query": long_query})
        assert result.startswith("WebSearch: ")
        assert result.endswith("...")
        assert len(result) <= 164  # "WebSearch: " (11) + 147 chars + "..." (3)

    def test_websearch_without_query(self):
        result = format_tool_preview("WebSearch", {})
        assert result == "WebSearch"

    # Task tool
    def test_task_with_description(self):
        result = format_tool_preview("Task", {"description": "Explore codebase"})
        assert result == "Task: Explore codebase"

    def test_task_without_description(self):
        result = format_tool_preview("Task", {})
        assert result == "Task"

    def test_task_with_other_fields(self):
        result = format_tool_preview("Task", {"description": "Run tests", "prompt": "test all"})
        assert result == "Task: Run tests"
