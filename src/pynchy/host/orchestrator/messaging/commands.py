"""Magic command word matching.

Detects special single-word or two-word commands (context reset, end session,
redeploy) using configurable word lists from config.toml. Also detects
approval gate commands (approve/deny/pending).
"""

from __future__ import annotations

import re

from pynchy.config import get_settings

# Matches 2-36 lowercase alphanumeric chars (short_id is 2, full UUID is 32-36)
_APPROVAL_ID_RE = re.compile(r"^[0-9a-z]{2,36}$")


def _strip_trigger(text: str) -> str:
    """Remove the leading trigger prefix (e.g. ``@pynchy``) if present.

    Slack normalises ``<@UBOTID>`` to ``@AgentName`` before the text reaches
    command detection.  A message like ``@pynchy c`` should be treated the
    same as a bare ``c``.
    """
    s = get_settings()
    return s.trigger_pattern.sub("", text).strip()


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
    text = _strip_trigger(text)
    verbs, nouns, aliases = _word_sets(get_settings().commands.reset)
    return _is_magic_command(text, verbs, nouns, aliases)


def is_end_session(text: str) -> bool:
    """Check if a message is an end session command."""
    text = _strip_trigger(text)
    verbs, nouns, aliases = _word_sets(get_settings().commands.end_session)
    return _is_magic_command(text, verbs, nouns, aliases)


def is_redeploy(text: str) -> bool:
    """Check if a message is a manual redeploy command."""
    text = _strip_trigger(text)
    w = get_settings().commands.redeploy
    word = text.strip().lower()
    aliases = set(w.aliases)
    verbs = set(w.verbs)
    return word in aliases or word in verbs


def is_any_magic_command(text: str) -> bool:
    """Check if a message matches any magic command (reset, end session, redeploy)."""
    return is_context_reset(text) or is_end_session(text) or is_redeploy(text)


# -- Approval gate commands ----------------------------------------------------


def is_approval_command(text: str) -> tuple[str, str] | None:
    """Check if text is an approve/deny command.

    Returns ``(action, short_id)`` or ``None``.
    Accepts bare ``approve <id>`` or with trigger prefix ``@pynchy approve <id>``.
    """
    text = _strip_trigger(text)
    words = text.strip().lower().split()
    if len(words) != 2:
        return None
    action, short_id = words
    if action not in ("approve", "deny"):
        return None
    if not _APPROVAL_ID_RE.match(short_id):
        return None
    return (action, short_id)


def is_pending_query(text: str) -> bool:
    """Check if text is a ``pending`` query command."""
    text = _strip_trigger(text)
    return text.strip().lower() == "pending"
