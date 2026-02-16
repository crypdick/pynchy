"""Tests for pynchy.commands — magic command word matching.

Validates the configurable single-word and two-word command detection
used for context reset, end session, and redeploy actions.
"""

from __future__ import annotations

from pynchy.commands import _is_magic_command, is_context_reset, is_end_session, is_redeploy

# ---------------------------------------------------------------------------
# _is_magic_command (generic matcher)
# ---------------------------------------------------------------------------


class TestIsMagicCommand:
    verbs = {"reset", "clear"}
    nouns = {"context", "session"}
    aliases = {"boom", "c"}

    def test_single_word_alias(self):
        assert _is_magic_command("boom", self.verbs, self.nouns, self.aliases)

    def test_single_word_alias_case_insensitive(self):
        assert _is_magic_command("BOOM", self.verbs, self.nouns, self.aliases)

    def test_single_word_alias_short(self):
        assert _is_magic_command("c", self.verbs, self.nouns, self.aliases)

    def test_verb_noun_pair(self):
        assert _is_magic_command("reset context", self.verbs, self.nouns, self.aliases)

    def test_noun_verb_pair(self):
        """Either word order should match."""
        assert _is_magic_command("context reset", self.verbs, self.nouns, self.aliases)

    def test_verb_noun_case_insensitive(self):
        assert _is_magic_command("RESET Context", self.verbs, self.nouns, self.aliases)

    def test_whitespace_trimmed(self):
        assert _is_magic_command("  boom  ", self.verbs, self.nouns, self.aliases)
        assert _is_magic_command("  reset context  ", self.verbs, self.nouns, self.aliases)

    def test_single_word_verb_no_match(self):
        """A verb alone (without a noun) should NOT match."""
        assert not _is_magic_command("reset", self.verbs, self.nouns, self.aliases)

    def test_single_word_noun_no_match(self):
        """A noun alone should NOT match."""
        assert not _is_magic_command("context", self.verbs, self.nouns, self.aliases)

    def test_three_words_no_match(self):
        assert not _is_magic_command("reset my context", self.verbs, self.nouns, self.aliases)

    def test_empty_string_no_match(self):
        assert not _is_magic_command("", self.verbs, self.nouns, self.aliases)

    def test_whitespace_only_no_match(self):
        assert not _is_magic_command("   ", self.verbs, self.nouns, self.aliases)

    def test_unrelated_text_no_match(self):
        assert not _is_magic_command("hello world", self.verbs, self.nouns, self.aliases)

    def test_partial_match_no_match(self):
        """A verb paired with a non-noun should NOT match."""
        assert not _is_magic_command("reset everything", self.verbs, self.nouns, self.aliases)


# ---------------------------------------------------------------------------
# is_context_reset
# ---------------------------------------------------------------------------


class TestIsContextReset:
    def test_verb_noun_combinations(self):
        assert is_context_reset("reset context")
        assert is_context_reset("clear session")
        assert is_context_reset("new conversation")
        assert is_context_reset("wipe chat")

    def test_reversed_word_order(self):
        assert is_context_reset("context reset")
        assert is_context_reset("session clear")

    def test_aliases(self):
        assert is_context_reset("boom")
        assert is_context_reset("c")

    def test_case_insensitive(self):
        assert is_context_reset("BOOM")
        assert is_context_reset("Reset Context")

    def test_not_triggered_by_partial(self):
        assert not is_context_reset("reset")
        assert not is_context_reset("context")

    def test_not_triggered_by_sentences(self):
        assert not is_context_reset("please reset context now")
        assert not is_context_reset("can you clear the session")


# ---------------------------------------------------------------------------
# is_end_session
# ---------------------------------------------------------------------------


class TestIsEndSession:
    def test_verb_noun_combinations(self):
        assert is_end_session("end session")
        assert is_end_session("stop session")
        assert is_end_session("close session")
        assert is_end_session("finish session")

    def test_reversed_word_order(self):
        assert is_end_session("session end")

    def test_aliases(self):
        assert is_end_session("done")
        assert is_end_session("bye")
        assert is_end_session("goodbye")
        assert is_end_session("cya")

    def test_case_insensitive(self):
        assert is_end_session("DONE")
        assert is_end_session("End Session")

    def test_not_triggered_by_partial(self):
        assert not is_end_session("end")
        assert not is_end_session("session")


# ---------------------------------------------------------------------------
# is_redeploy
# ---------------------------------------------------------------------------


class TestIsRedeploy:
    def test_aliases(self):
        assert is_redeploy("r")

    def test_verbs(self):
        assert is_redeploy("redeploy")
        assert is_redeploy("deploy")

    def test_case_insensitive(self):
        assert is_redeploy("R")
        assert is_redeploy("REDEPLOY")
        assert is_redeploy("Deploy")

    def test_whitespace_trimmed(self):
        assert is_redeploy("  r  ")
        assert is_redeploy("  redeploy  ")

    def test_not_triggered_by_sentences(self):
        assert not is_redeploy("please redeploy now")
        assert not is_redeploy("r now")

    def test_not_triggered_by_unrelated(self):
        assert not is_redeploy("hello")
        assert not is_redeploy("")
