"""Magic command word matching.

Detects special single-word or two-word commands (context reset, end session,
redeploy) using configurable word lists from config.toml.
"""

from __future__ import annotations

from pynchy.config import get_settings


def _is_magic_command(
    text: str,
    verbs: set[str],
    nouns: set[str],
    aliases: set[str],
) -> bool:
    """Check if text matches a verb+noun pair (either order) or a single alias."""
    words = text.strip().lower().split()
    if len(words) == 1:
        return words[0] in aliases
    if len(words) == 2:
        a, b = words
        return (a in verbs and b in nouns) or (a in nouns and b in verbs)
    return False


def _word_sets(w: object) -> tuple[set[str], set[str], set[str]]:
    """Return (verbs, nouns, aliases) as frozen sets, avoiding repeated conversion."""
    return set(w.verbs), set(w.nouns), set(w.aliases)  # type: ignore[attr-defined]


def is_context_reset(text: str) -> bool:
    """Check if a message is a context reset command."""
    verbs, nouns, aliases = _word_sets(get_settings().commands.reset)
    return _is_magic_command(text, verbs, nouns, aliases)


def is_end_session(text: str) -> bool:
    """Check if a message is an end session command."""
    verbs, nouns, aliases = _word_sets(get_settings().commands.end_session)
    return _is_magic_command(text, verbs, nouns, aliases)


def is_redeploy(text: str) -> bool:
    """Check if a message is a manual redeploy command."""
    w = get_settings().commands.redeploy
    word = text.strip().lower()
    aliases = set(w.aliases)
    verbs = set(w.verbs)
    return word in aliases or word in verbs


def is_any_magic_command(text: str) -> bool:
    """Check if a message matches any magic command (reset, end session, redeploy)."""
    return is_context_reset(text) or is_end_session(text) or is_redeploy(text)
