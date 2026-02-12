"""Tests for XML escaping, message formatting, trigger pattern, and outbound formatting.

Port of src/formatting.test.ts.
"""

from __future__ import annotations

from dataclasses import dataclass

from pynchy.config import ASSISTANT_NAME, TRIGGER_PATTERN
from pynchy.router import escape_xml, format_messages, format_outbound, strip_internal_tags
from pynchy.types import NewMessage

# --- escapeXml ---


class TestEscapeXml:
    def test_escapes_ampersands(self):
        assert escape_xml("a & b") == "a &amp; b"

    def test_escapes_less_than(self):
        assert escape_xml("a < b") == "a &lt; b"

    def test_escapes_greater_than(self):
        assert escape_xml("a > b") == "a &gt; b"

    def test_escapes_double_quotes(self):
        assert escape_xml('"hello"') == "&quot;hello&quot;"

    def test_handles_multiple_special_characters(self):
        assert escape_xml('a & b < c > d "e"') == "a &amp; b &lt; c &gt; d &quot;e&quot;"

    def test_passes_through_no_special_chars(self):
        assert escape_xml("hello world") == "hello world"

    def test_handles_empty_string(self):
        assert escape_xml("") == ""


# --- formatMessages ---


class TestFormatMessages:
    def test_formats_single_message_as_xml(self, make_msg):
        result = format_messages([make_msg()])
        assert result == (
            "<messages>\n"
            '<message sender="Alice" time="2024-01-01T00:00:00.000Z">hello</message>\n'
            "</messages>"
        )

    def test_formats_multiple_messages(self, make_msg):
        msgs = [
            make_msg(id="1", sender_name="Alice", content="hi", timestamp="t1"),
            make_msg(id="2", sender_name="Bob", content="hey", timestamp="t2"),
        ]
        result = format_messages(msgs)
        assert 'sender="Alice"' in result
        assert 'sender="Bob"' in result
        assert ">hi</message>" in result
        assert ">hey</message>" in result

    def test_escapes_special_chars_in_sender_names(self, make_msg):
        result = format_messages([make_msg(sender_name="A & B <Co>")])
        assert 'sender="A &amp; B &lt;Co&gt;"' in result

    def test_escapes_special_chars_in_content(self, make_msg):
        result = format_messages([make_msg(content='<script>alert("xss")</script>')])
        assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in result

    def test_handles_empty_array(self):
        result = format_messages([])
        assert result == "<messages>\n\n</messages>"


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

    def test_prefixes_with_assistant_name(self):
        ch = self._FakeChannel(prefix_assistant_name=True)
        assert format_outbound(ch, "hello world") == f"{ASSISTANT_NAME}: hello world"

    def test_does_not_prefix_when_opted_out(self):
        ch = self._FakeChannel(prefix_assistant_name=False)
        assert format_outbound(ch, "hello world") == "hello world"

    def test_defaults_to_prefixing_when_undefined(self):
        ch = self._FakeChannel(prefix_assistant_name=None)
        assert format_outbound(ch, "hello world") == f"{ASSISTANT_NAME}: hello world"

    def test_returns_empty_when_all_internal(self):
        ch = self._FakeChannel(prefix_assistant_name=True)
        assert format_outbound(ch, "<internal>hidden</internal>") == ""

    def test_strips_internal_and_prefixes_remaining(self):
        ch = self._FakeChannel(prefix_assistant_name=True)
        result = format_outbound(ch, "<internal>thinking</internal>The answer is 42")
        assert result == f"{ASSISTANT_NAME}: The answer is 42"


# --- Trigger gating with requiresTrigger flag ---


class TestTriggerGating:
    """Replicates the trigger gating logic from the orchestrator."""

    @staticmethod
    def _should_require_trigger(is_main_group: bool, requires_trigger: bool | None) -> bool:
        return not is_main_group and requires_trigger is not False

    @staticmethod
    def _should_process(
        is_main_group: bool,
        requires_trigger: bool | None,
        messages: list[NewMessage],
    ) -> bool:
        if not TestTriggerGating._should_require_trigger(is_main_group, requires_trigger):
            return True
        return any(TRIGGER_PATTERN.search(m.content.strip()) for m in messages)

    def test_main_group_always_processes(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(True, None, msgs)

    def test_main_group_processes_even_with_requires_trigger_true(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(True, True, msgs)

    def test_non_main_defaults_to_requiring_trigger(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert not self._should_process(False, None, msgs)

    def test_non_main_with_requires_trigger_true(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert not self._should_process(False, True, msgs)

    def test_non_main_processes_when_trigger_present(self, make_msg):
        msgs = [make_msg(content="@pynchy do something")]
        assert self._should_process(False, True, msgs)

    def test_non_main_with_requires_trigger_false_always_processes(self, make_msg):
        msgs = [make_msg(content="hello no trigger")]
        assert self._should_process(False, False, msgs)
