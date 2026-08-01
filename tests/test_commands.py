"""Tests for pynchy.commands — magic command word matching.

Validates the configurable single-word and two-word command detection
used for context reset, end session, and redeploy actions.
"""

from __future__ import annotations

from functools import partial

from conftest import make_command_matcher, make_settings

from pynchy.host.orchestrator.messaging import commands

_MATCHER = make_command_matcher(make_settings())
is_context_reset = partial(commands.is_context_reset, _MATCHER)
is_end_session = partial(commands.is_end_session, _MATCHER)
is_pause = partial(commands.is_pause, _MATCHER)
is_redeploy = partial(commands.is_redeploy, _MATCHER)

# ---------------------------------------------------------------------------
# verb+noun / alias matching, exercised through the public is_context_reset.
# Default reset config: verbs include reset/clear, nouns include context/session,
# aliases include boom/c/new/clear/reset.
# ---------------------------------------------------------------------------


class TestIsMagicCommand:
    def test_single_word_alias(self):
        assert is_context_reset("boom")

    def test_single_word_alias_case_insensitive(self):
        assert is_context_reset("BOOM")

    def test_single_word_alias_short(self):
        assert is_context_reset("c")

    def test_single_word_reset_aliases(self):
        assert is_context_reset("new")
        assert is_context_reset("clear")
        assert is_context_reset("reset")

    def test_verb_noun_pair(self):
        assert is_context_reset("reset context")

    def test_noun_verb_pair(self):
        """Either word order should match."""
        assert is_context_reset("context reset")

    def test_verb_noun_case_insensitive(self):
        assert is_context_reset("RESET Context")

    def test_whitespace_trimmed(self):
        assert is_context_reset("  boom  ")
        assert is_context_reset("  reset context  ")

    def test_other_single_word_verb_no_match(self):
        """A verb alone (without a noun) should NOT match."""
        assert not is_context_reset("wipe")

    def test_single_word_noun_no_match(self):
        """A noun alone should NOT match."""
        assert not is_context_reset("context")

    def test_three_words_no_match(self):
        assert not is_context_reset("reset my context")

    def test_empty_string_no_match(self):
        assert not is_context_reset("")

    def test_whitespace_only_no_match(self):
        assert not is_context_reset("   ")

    def test_unrelated_text_no_match(self):
        assert not is_context_reset("hello world")

    def test_partial_match_no_match(self):
        """A verb paired with a non-noun should NOT match."""
        assert not is_context_reset("reset everything")


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
        assert is_context_reset("new")
        assert is_context_reset("clear")
        assert is_context_reset("reset")

    def test_case_insensitive(self):
        assert is_context_reset("BOOM")
        assert is_context_reset("Reset Context")

    def test_trigger_prefix_stripped(self):
        """Commands prefixed with @trigger (e.g. from Slack) should still match."""
        assert is_context_reset("@pynchy c")
        assert is_context_reset("@pynchy boom")
        assert is_context_reset("@pynchy new")
        assert is_context_reset("@pynchy clear")
        assert is_context_reset("@pynchy reset")
        assert is_context_reset("@pynchy clear context")
        assert is_context_reset("@pynchy context reset")

    def test_trigger_alias_prefix_stripped(self):
        """Trigger aliases (e.g. @ghost) should also be stripped."""
        assert is_context_reset("@ghost c")
        assert is_context_reset("@ghost clear context")

    def test_not_triggered_by_partial(self):
        assert not is_context_reset("restart")
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

    def test_trigger_prefix_stripped(self):
        """Commands prefixed with @trigger should still match."""
        assert is_end_session("@pynchy done")
        assert is_end_session("@pynchy end session")

    def test_not_triggered_by_partial(self):
        assert not is_end_session("end")
        assert not is_end_session("session")


# ---------------------------------------------------------------------------
# is_pause
# ---------------------------------------------------------------------------


class TestIsPause:
    def test_aliases_are_exact_and_case_insensitive(self):
        assert is_pause("stop")
        assert is_pause("PAUSE")
        assert is_pause("  Pause  ")

    def test_trigger_prefixes_are_stripped(self):
        assert is_pause("@pynchy pause")
        assert is_pause("@ghost STOP")

    def test_sentences_and_partial_matches_are_rejected(self):
        assert not is_pause("please pause")
        assert not is_pause("stop now")
        assert not is_pause("stopping")
        assert not is_pause("paused")

    def test_does_not_conflict_with_existing_lifecycle_commands(self):
        assert not is_pause("reset context")
        assert not is_pause("end session")
        assert not is_pause("done")
        assert not is_end_session("stop")


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

    def test_trigger_prefix_stripped(self):
        """Commands prefixed with @trigger should still match."""
        assert is_redeploy("@pynchy r")
        assert is_redeploy("@pynchy redeploy")

    def test_not_triggered_by_sentences(self):
        assert not is_redeploy("please redeploy now")
        assert not is_redeploy("r now")

    def test_not_triggered_by_unrelated(self):
        assert not is_redeploy("hello")
        assert not is_redeploy("")


class TestApplicationCommands:
    def test_malformed_application_command_metadata_is_ignored(self):
        metadata = {
            "application_command": {"name": "approve", "options": []},
        }

        assert commands.is_approval_command(_MATCHER, "hello", metadata) is None

    def test_lifecycle_commands_use_intent_metadata(self):
        def metadata(name: str) -> dict[str, object]:
            return {
                "application_commands": True,
                "application_command": {"name": name},
            }

        assert is_pause("/pause", metadata("pause"))
        assert is_context_reset("/reset", metadata("reset"))
        assert is_end_session("/end-session", metadata("end_session"))
        assert is_redeploy("/redeploy", metadata("redeploy"))

    def test_approval_commands_read_the_short_id_option(self):
        approve = {
            "application_commands": True,
            "application_command": {"name": "approve", "options": {"short_id": "abc123"}},
        }
        deny = {
            "application_commands": True,
            "application_command": {"name": "deny", "options": {"short_id": "abc123"}},
        }

        assert commands.is_approval_command(_MATCHER, "/approve abc123", approve) == (
            "approve",
            "abc123",
        )
        assert commands.is_approval_command(_MATCHER, "/deny abc123", deny) == (
            "deny",
            "abc123",
        )
        assert commands.is_pending_query(
            _MATCHER,
            "/pending",
            {"application_commands": True, "application_command": {"name": "pending"}},
        )

    def test_discord_message_text_does_not_trigger_controls(self):
        metadata = {"application_commands": True}

        assert not is_context_reset("reset", metadata)
        assert not is_pause("pause", metadata)
        assert not is_end_session("done", metadata)
        assert not is_redeploy("redeploy", metadata)

        assert commands.is_approval_command(_MATCHER, "approve abc123", metadata) is None
        assert not commands.is_pending_query(_MATCHER, "pending", metadata)
