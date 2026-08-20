"""Human control command matching.

Detects special single-word or two-word commands (pause, context reset, end
session, redeploy) using configurable word lists from layered settings. Also detects
approval gate commands (approve/deny/pending).
"""

from __future__ import annotations

import re
from typing import Any

from pynchy.host.orchestrator.messaging.deps import (  # noqa: TC001 - beartype resolves matcher annotations at runtime.
    CommandMatcher,
)

# Matches 2-36 lowercase alphanumeric chars (short_id is 2, full UUID is 32-36)
_APPROVAL_ID_RE = re.compile(r"^[0-9a-z]{2,36}$")
_APPLICATION_COMMAND_KEY = "application_command"


def _strip_trigger(matcher: CommandMatcher, text: str) -> str:
    """Remove the leading trigger prefix (e.g. ``@pynchy``) if present.

    Slack normalises ``<@UBOTID>`` to ``@AgentName`` before the text reaches
    command detection.  A message like ``@pynchy c`` should be treated the
    same as a bare ``c``.
    """
    return str(matcher.trigger_pattern.sub("", text).strip())


def _application_command(metadata: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """Return a channel command intent when one was attached at intake."""
    raw = (metadata or {}).get(_APPLICATION_COMMAND_KEY)
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    options = raw.get("options", {})
    if not isinstance(name, str) or not isinstance(options, dict):
        return None
    return name, options


def _uses_application_commands(metadata: dict[str, Any] | None) -> bool:
    return (metadata or {}).get("application_commands") is True


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


def is_context_reset(
    matcher: CommandMatcher, text: str, metadata: dict[str, Any] | None = None
) -> bool:
    """Check if a message is a context reset command."""
    if (command := _application_command(metadata)) is not None:
        return command[0] == "reset"
    if _uses_application_commands(metadata):
        return False
    text = _strip_trigger(matcher, text)
    return _is_magic_command(text, matcher.reset.verbs, matcher.reset.nouns, matcher.reset.aliases)


def is_end_session(
    matcher: CommandMatcher, text: str, metadata: dict[str, Any] | None = None
) -> bool:
    """Check if a message is an end session command."""
    if (command := _application_command(metadata)) is not None:
        return command[0] == "end_session"
    if _uses_application_commands(metadata):
        return False
    text = _strip_trigger(matcher, text)
    return _is_magic_command(
        text,
        matcher.end_session.verbs,
        matcher.end_session.nouns,
        matcher.end_session.aliases,
    )


def is_redeploy(matcher: CommandMatcher, text: str, metadata: dict[str, Any] | None = None) -> bool:
    """Check if a message is a manual redeploy command."""
    if (command := _application_command(metadata)) is not None:
        return command[0] == "redeploy"
    if _uses_application_commands(metadata):
        return False
    text = _strip_trigger(matcher, text)
    word = text.strip().lower()
    return word in matcher.redeploy.aliases or word in matcher.redeploy.verbs


def is_pause(matcher: CommandMatcher, text: str, metadata: dict[str, Any] | None = None) -> bool:
    """Check if a message is an exact resumable-pause command."""
    if (command := _application_command(metadata)) is not None:
        return command[0] == "pause"
    if _uses_application_commands(metadata):
        return False
    text = _strip_trigger(matcher, text)
    word = text.strip().lower()
    return word in matcher.pause.aliases


def is_any_magic_command(
    matcher: CommandMatcher, text: str, metadata: dict[str, Any] | None = None
) -> bool:
    """Check if a message matches any lifecycle magic command."""
    return (
        is_pause(matcher, text, metadata)
        or is_context_reset(matcher, text, metadata)
        or is_end_session(matcher, text, metadata)
        or is_redeploy(matcher, text, metadata)
    )


# -- Approval gate commands ----------------------------------------------------


def is_approval_command(
    matcher: CommandMatcher,
    text: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, str] | None:
    """Check if text is an approve/deny command.

    Returns ``(action, short_id)`` or ``None``.
    Accepts bare ``approve <id>`` or with trigger prefix ``@pynchy approve <id>``.
    """
    if (command := _application_command(metadata)) is not None:
        action, options = command
        short_id = options.get("short_id")
        return (
            (action, short_id)
            if action in ("approve", "approve-once", "approve-session", "approve-forever", "deny")
            and isinstance(short_id, str)
            and _APPROVAL_ID_RE.match(short_id)
            else None
        )
    if _uses_application_commands(metadata):
        return None
    text = _strip_trigger(matcher, text)
    words = text.strip().lower().split()
    if len(words) != 2:
        return None
    action, short_id = words
    if action not in (
        "approve",
        "approve-once",
        "approve-session",
        "approve-forever",
        "deny",
    ):
        return None
    if not _APPROVAL_ID_RE.match(short_id):
        return None
    return (action, short_id)


def is_pending_query(
    matcher: CommandMatcher, text: str, metadata: dict[str, Any] | None = None
) -> bool:
    """Check if text is a ``pending`` query command."""
    if (command := _application_command(metadata)) is not None:
        return command[0] == "pending"
    if _uses_application_commands(metadata):
        return False
    text = _strip_trigger(matcher, text)
    return text.strip().lower() == "pending"
