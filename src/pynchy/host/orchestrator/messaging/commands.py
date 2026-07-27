"""Magic command word matching.

Detects special single-word or two-word commands (pause, context reset, end
session, redeploy) using configurable word lists from layered settings. Also detects
approval gate commands (approve/deny/pending).
"""

from __future__ import annotations

import re

from pynchy.host.orchestrator.messaging.deps import (  # noqa: TC001, RUF100 - beartype resolves matcher annotations at runtime.
    CommandMatcher,
)

# Matches 2-36 lowercase alphanumeric chars (short_id is 2, full UUID is 32-36)
_APPROVAL_ID_RE = re.compile(r"^[0-9a-z]{2,36}$")


def _strip_trigger(matcher: CommandMatcher, text: str) -> str:
    """Remove the leading trigger prefix (e.g. ``@pynchy``) if present.

    Slack normalises ``<@UBOTID>`` to ``@AgentName`` before the text reaches
    command detection.  A message like ``@pynchy c`` should be treated the
    same as a bare ``c``.
    """
    return str(matcher.trigger_pattern.sub("", text).strip())


def _is_magic_command(
    text: str,
    verbs: frozenset[str],
    nouns: frozenset[str],
    aliases: frozenset[str],
) -> bool:
    """Check if text matches a verb+noun pair (either order) or a single alias."""
    words = text.strip().lower().split()
    if len(words) == 1:
        return words[0] in aliases
    if len(words) == 2:
        a, b = words
        return (a in verbs and b in nouns) or (a in nouns and b in verbs)
    return False


def is_context_reset(matcher: CommandMatcher, text: str) -> bool:
    """Check if a message is a context reset command."""
    text = _strip_trigger(matcher, text)
    return _is_magic_command(text, matcher.reset.verbs, matcher.reset.nouns, matcher.reset.aliases)


def is_end_session(matcher: CommandMatcher, text: str) -> bool:
    """Check if a message is an end session command."""
    text = _strip_trigger(matcher, text)
    return _is_magic_command(
        text,
        matcher.end_session.verbs,
        matcher.end_session.nouns,
        matcher.end_session.aliases,
    )


def is_redeploy(matcher: CommandMatcher, text: str) -> bool:
    """Check if a message is a manual redeploy command."""
    text = _strip_trigger(matcher, text)
    word = text.strip().lower()
    return word in matcher.redeploy.aliases or word in matcher.redeploy.verbs


def is_pause(matcher: CommandMatcher, text: str) -> bool:
    """Check if a message is an exact resumable-pause command."""
    text = _strip_trigger(matcher, text)
    word = text.strip().lower()
    return word in matcher.pause.aliases


def is_any_magic_command(matcher: CommandMatcher, text: str) -> bool:
    """Check if a message matches any lifecycle magic command."""
    return (
        is_pause(matcher, text)
        or is_context_reset(matcher, text)
        or is_end_session(matcher, text)
        or is_redeploy(matcher, text)
    )


# -- Approval gate commands ----------------------------------------------------


def is_approval_command(matcher: CommandMatcher, text: str) -> tuple[str, str] | None:
    """Check if text is an approve/deny command.

    Returns ``(action, short_id)`` or ``None``.
    Accepts bare ``approve <id>`` or with trigger prefix ``@pynchy approve <id>``.
    """
    text = _strip_trigger(matcher, text)
    words = text.strip().lower().split()
    if len(words) != 2:
        return None
    action, short_id = words
    if action not in ("approve", "deny"):
        return None
    if not _APPROVAL_ID_RE.match(short_id):
        return None
    return (action, short_id)


def is_pending_query(matcher: CommandMatcher, text: str) -> bool:
    """Check if text is a ``pending`` query command."""
    text = _strip_trigger(matcher, text)
    return text.strip().lower() == "pending"
