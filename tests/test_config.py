"""Tests for configuration logic.

Tests critical business logic in config.py: context reset, end session, and redeploy detection.
"""

from __future__ import annotations

from unittest.mock import patch

from pynchy.commands import is_context_reset, is_end_session, is_redeploy
from pynchy.config import _detect_timezone


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

    # Alias 'c' for quick context reset
    def test_recognizes_c_alias(self):
        assert is_context_reset("c")

    def test_c_alias_is_case_insensitive(self):
        assert is_context_reset("C")

    def test_c_alias_with_whitespace(self):
        assert is_context_reset("  c  ")


class TestIsEndSession:
    """Test the is_end_session() function which determines if a message
    should trigger a graceful session end (sync + spindown, no context wipe).
    """

    # Single-word aliases
    def test_recognizes_done_alias(self):
        assert is_end_session("done")

    def test_recognizes_bye_alias(self):
        assert is_end_session("bye")

    def test_recognizes_goodbye_alias(self):
        assert is_end_session("goodbye")

    def test_recognizes_cya_alias(self):
        assert is_end_session("cya")

    # Two-word combinations: verb + noun
    def test_end_session(self):
        assert is_end_session("end session")

    def test_stop_session(self):
        assert is_end_session("stop session")

    def test_close_session(self):
        assert is_end_session("close session")

    def test_finish_session(self):
        assert is_end_session("finish session")

    # Reversed order
    def test_session_end(self):
        assert is_end_session("session end")

    def test_session_stop(self):
        assert is_end_session("session stop")

    def test_session_close(self):
        assert is_end_session("session close")

    # Case insensitivity
    def test_case_insensitive(self):
        assert is_end_session("END SESSION")
        assert is_end_session("End Session")
        assert is_end_session("DONE")
        assert is_end_session("Bye")

    # Whitespace handling
    def test_handles_extra_whitespace(self):
        assert is_end_session("  done  ")
        assert is_end_session("  end   session  ")

    # Negative cases
    def test_does_not_match_wrong_noun(self):
        assert not is_end_session("end context")
        assert not is_end_session("stop chat")
        assert not is_end_session("close conversation")

    def test_does_not_match_three_words(self):
        assert not is_end_session("end the session")
        assert not is_end_session("please end session")

    def test_does_not_match_single_keywords(self):
        assert not is_end_session("end")
        assert not is_end_session("stop")
        assert not is_end_session("session")

    def test_empty_string(self):
        assert not is_end_session("")

    def test_whitespace_only(self):
        assert not is_end_session("   ")

    def test_does_not_overlap_with_context_reset(self):
        """End session aliases should not trigger context reset and vice versa."""
        # End session words should NOT trigger context reset
        assert not is_context_reset("done")
        assert not is_context_reset("bye")
        assert not is_context_reset("end session")

        # Context reset words should NOT trigger end session
        assert not is_end_session("boom")
        assert not is_end_session("reset context")
        assert not is_end_session("new session")


class TestIsRedeploy:
    """Test the is_redeploy() function which detects manual redeploy commands.

    Critical because incorrect detection could either cause unwanted service
    restarts (false positive) or prevent intentional deploys (false negative).
    """

    # Valid aliases
    def test_recognizes_r_alias(self):
        assert is_redeploy("r")

    def test_recognizes_redeploy(self):
        assert is_redeploy("redeploy")

    def test_recognizes_deploy(self):
        assert is_redeploy("deploy")

    # Case insensitivity
    def test_case_insensitive_r(self):
        assert is_redeploy("R")

    def test_case_insensitive_redeploy(self):
        assert is_redeploy("REDEPLOY")
        assert is_redeploy("Redeploy")
        assert is_redeploy("rEdEpLoY")

    def test_case_insensitive_deploy(self):
        assert is_redeploy("DEPLOY")
        assert is_redeploy("Deploy")

    # Whitespace handling
    def test_handles_leading_trailing_whitespace(self):
        assert is_redeploy("  r  ")
        assert is_redeploy("  redeploy  ")
        assert is_redeploy("\n deploy \t")

    # Negative cases
    def test_does_not_match_empty_string(self):
        assert not is_redeploy("")

    def test_does_not_match_whitespace_only(self):
        assert not is_redeploy("   ")

    def test_does_not_match_partial_words(self):
        assert not is_redeploy("redeploying")
        assert not is_redeploy("redeployed")

    def test_does_not_match_sentences(self):
        assert not is_redeploy("please redeploy")
        assert not is_redeploy("redeploy now")
        assert not is_redeploy("r now")

    def test_does_not_match_similar_words(self):
        assert not is_redeploy("restart")
        assert not is_redeploy("reset")
        assert not is_redeploy("re")

    def test_does_not_match_r_as_substring(self):
        """Single letter 'r' shouldn't match within longer words."""
        assert not is_redeploy("run")
        assert not is_redeploy("read")


# ---------------------------------------------------------------------------
# _detect_timezone
# ---------------------------------------------------------------------------


class TestDetectTimezone:
    """Test timezone auto-detection from environment and system config.

    Correct timezone detection is critical for scheduled tasks —
    wrong timezone causes tasks to fire at unexpected times.
    """

    def test_tz_env_var_takes_priority(self):
        with patch.dict("os.environ", {"TZ": "America/New_York"}):
            assert _detect_timezone() == "America/New_York"

    def test_tz_env_var_empty_falls_through(self):
        """Empty TZ env var is falsy, should fall through to readlink."""
        with (
            patch.dict("os.environ", {"TZ": ""}, clear=False),
            patch("pynchy.config.os.readlink", side_effect=OSError("not a link")),
        ):
            assert _detect_timezone() == "UTC"

    def test_reads_localtime_symlink(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "pynchy.config.os.readlink",
                return_value="/usr/share/zoneinfo/Europe/London",
            ),
        ):
            assert _detect_timezone() == "Europe/London"

    def test_localtime_symlink_deep_path(self):
        """Handles zoneinfo paths with multiple components after zoneinfo/."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "pynchy.config.os.readlink",
                return_value="/usr/share/zoneinfo/America/Argentina/Buenos_Aires",
            ),
        ):
            assert _detect_timezone() == "America/Argentina/Buenos_Aires"

    def test_localtime_not_a_symlink(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pynchy.config.os.readlink", side_effect=OSError("not a symlink")),
        ):
            assert _detect_timezone() == "UTC"

    def test_localtime_symlink_without_zoneinfo(self):
        """Symlink target that doesn't contain 'zoneinfo/' falls back to UTC."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch(
                "pynchy.config.os.readlink",
                return_value="/some/other/path/timezone",
            ),
        ):
            assert _detect_timezone() == "UTC"

    def test_defaults_to_utc(self):
        """No TZ and no /etc/localtime → defaults to UTC."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("pynchy.config.os.readlink", side_effect=FileNotFoundError),
        ):
            assert _detect_timezone() == "UTC"
