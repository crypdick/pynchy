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


def is_context_reset(text: str) -> bool:
    """Check if a message is a context reset command."""
    w = get_settings().commands.reset
    return _is_magic_command(text, set(w.verbs), set(w.nouns), set(w.aliases))


def is_end_session(text: str) -> bool:
    """Check if a message is an end session command."""
    w = get_settings().commands.end_session
    return _is_magic_command(text, set(w.verbs), set(w.nouns), set(w.aliases))


def is_redeploy(text: str) -> bool:
    """Check if a message is a manual redeploy command."""
    w = get_settings().commands.redeploy
    word = text.strip().lower()
    return word in set(w.aliases) or word in set(w.verbs)
