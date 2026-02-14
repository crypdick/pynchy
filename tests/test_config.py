"""Tests for configuration logic.

Tests critical business logic in config.py, especially context reset detection.
"""

from __future__ import annotations

from pynchy.config import is_context_reset


class TestIsContextReset:
    """Test the is_context_reset() function which determines if a message
    should trigger a conversation context reset.

    This is critical business logic - incorrect detection could cause data loss
    (resetting when user didn't want to) or confusion (not resetting when they did).
    """

    # Single-word aliases
    def test_recognizes_boom_alias(self):
        assert is_context_reset("boom")

    def test_boom_is_case_insensitive(self):
        assert is_context_reset("BOOM")
        assert is_context_reset("Boom")

    def test_boom_with_whitespace(self):
        assert is_context_reset("  boom  ")

    def test_boom_does_not_match_as_substring(self):
        assert not is_context_reset("boom boom")
        assert not is_context_reset("kaboom")
        assert not is_context_reset("boom!")

    # Two-word combinations: verb + noun
    def test_reset_context(self):
        assert is_context_reset("reset context")

    def test_reset_session(self):
        assert is_context_reset("reset session")

    def test_reset_chat(self):
        assert is_context_reset("reset chat")

    def test_reset_conversation(self):
        assert is_context_reset("reset conversation")

    def test_restart_context(self):
        assert is_context_reset("restart context")

    def test_clear_context(self):
        assert is_context_reset("clear context")

    def test_new_session(self):
        assert is_context_reset("new session")

    def test_wipe_context(self):
        assert is_context_reset("wipe context")

    # Reversed order (noun + verb)
    def test_context_reset(self):
        assert is_context_reset("context reset")

    def test_session_restart(self):
        assert is_context_reset("session restart")

    def test_chat_clear(self):
        assert is_context_reset("chat clear")

    def test_conversation_wipe(self):
        assert is_context_reset("conversation wipe")

    # Case insensitivity
    def test_case_insensitive_reset_context(self):
        assert is_context_reset("RESET CONTEXT")
        assert is_context_reset("Reset Context")
        assert is_context_reset("rEsEt CoNtExT")

    def test_case_insensitive_context_reset(self):
        assert is_context_reset("CONTEXT RESET")
        assert is_context_reset("Context Reset")

    # Whitespace handling
    def test_handles_extra_whitespace(self):
        assert is_context_reset("  reset   context  ")
        assert is_context_reset("\treset\tcontext\t")
        assert is_context_reset("\n reset context \n")

    # Negative cases - should NOT trigger reset
    def test_does_not_match_partial_words(self):
        assert not is_context_reset("unreset context")
        assert not is_context_reset("reset contextual")
        assert not is_context_reset("resetting context")

    def test_does_not_match_three_words(self):
        assert not is_context_reset("reset the context")
        assert not is_context_reset("please reset context")

    def test_does_not_match_wrong_combinations(self):
        # Valid verb, wrong noun
        assert not is_context_reset("reset memory")
        assert not is_context_reset("reset history")

        # Valid noun, wrong verb
        assert not is_context_reset("delete context")
        assert not is_context_reset("remove session")

    def test_does_not_match_single_word_from_pair(self):
        assert not is_context_reset("reset")
        assert not is_context_reset("context")
        assert not is_context_reset("session")
        assert not is_context_reset("restart")

    def test_empty_string(self):
        assert not is_context_reset("")

    def test_whitespace_only(self):
        assert not is_context_reset("   ")
        assert not is_context_reset("\t\n")

    def test_does_not_match_sentences(self):
        assert not is_context_reset("I want to reset the context please")
        assert not is_context_reset("Can you reset context for me?")

    # Edge cases with valid aliases
    def test_all_valid_single_aliases(self):
        """Verify all documented single-word aliases work."""
        for alias in ["boom"]:
            assert is_context_reset(alias), f"Failed for alias: {alias}"

    def test_all_verb_noun_combinations(self):
        """Verify key verb+noun combinations work.

        This tests a representative sample from the cartesian product
        of _RESET_VERBS × _RESET_NOUNS to ensure the logic works correctly.
        """
        test_cases = [
            ("reset", "context"),
            ("restart", "session"),
            ("clear", "chat"),
            ("new", "conversation"),
            ("wipe", "context"),
            # Reversed
            ("context", "reset"),
            ("session", "restart"),
            ("chat", "clear"),
        ]

        for verb, noun in test_cases:
            assert is_context_reset(f"{verb} {noun}"), f"Failed for: {verb} {noun}"
