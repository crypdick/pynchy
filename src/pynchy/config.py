"""Configuration constants and paths.

Port of src/config.ts — all time intervals in seconds, all paths as pathlib.Path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME", "pynchy")

# Intervals in seconds (TS uses milliseconds)
POLL_INTERVAL: float = 2.0
SCHEDULER_POLL_INTERVAL: float = 60.0
IPC_POLL_INTERVAL: float = 1.0

# Paths
PROJECT_ROOT: Path = Path.cwd()
HOME_DIR: Path = Path(os.environ.get("HOME", "/Users/user"))

MOUNT_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "pynchy" / "mount-allowlist.json"
WORKTREES_DIR: Path = HOME_DIR / ".config" / "pynchy" / "worktrees"
STORE_DIR: Path = (PROJECT_ROOT / "store").resolve()
GROUPS_DIR: Path = (PROJECT_ROOT / "groups").resolve()
DATA_DIR: Path = (PROJECT_ROOT / "data").resolve()
# Container settings (time values converted from ms to seconds)
DEFAULT_AGENT_CORE: str = os.environ.get("PYNCHY_AGENT_CORE", "claude")
CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE", "pynchy-agent:latest")
CONTAINER_TIMEOUT: float = int(os.environ.get("CONTAINER_TIMEOUT", "1800000")) / 1000
CONTAINER_MAX_OUTPUT_SIZE: int = int(
    os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760")
)  # 10MB
IDLE_TIMEOUT: float = int(os.environ.get("IDLE_TIMEOUT", "1800000")) / 1000  # 30min
DEPLOY_PORT: int = int(os.environ.get("DEPLOY_PORT", "8484"))
try:
    MAX_CONCURRENT_CONTAINERS: int = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))
except ValueError:
    MAX_CONCURRENT_CONTAINERS: int = 5

# Sentinel markers for robust output parsing (must match agent-runner)
OUTPUT_START_MARKER = "---PYNCHY_OUTPUT_START---"
OUTPUT_END_MARKER = "---PYNCHY_OUTPUT_END---"


def _escape_regex(s: str) -> str:
    return re.escape(s)


# Additional trigger aliases (case insensitive, matched alongside ASSISTANT_NAME)
TRIGGER_ALIASES: list[str] = [
    s for s in os.environ.get("TRIGGER_ALIASES", "ghost").split(",") if s.strip()
]

_trigger_names = [_escape_regex(ASSISTANT_NAME)] + [
    _escape_regex(a.strip()) for a in TRIGGER_ALIASES
]
TRIGGER_PATTERN: re.Pattern[str] = re.compile(rf"^@({'|'.join(_trigger_names)})\b", re.IGNORECASE)

# Magic words to reset conversation context (voice-friendly variants)
_RESET_VERBS = {"reset", "restart", "clear", "new", "wipe"}
_RESET_NOUNS = {"context", "session", "chat", "conversation"}
_RESET_ALIASES = {"boom", "c"}

# Magic words to end session without clearing context.
# Syncs worktree and spins down container, but preserves conversation history.
# Next message will start a fresh container with the existing session context.
_END_SESSION_VERBS = {"end", "stop", "close", "finish"}
_END_SESSION_NOUNS = {"session"}
_END_SESSION_ALIASES = {"done", "bye", "goodbye", "cya"}


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
    return _is_magic_command(text, _RESET_VERBS, _RESET_NOUNS, _RESET_ALIASES)


def is_end_session(text: str) -> bool:
    """Check if a message is an end session command."""
    return _is_magic_command(text, _END_SESSION_VERBS, _END_SESSION_NOUNS, _END_SESSION_ALIASES)


_REDEPLOY_ALIASES = {"r"}
_REDEPLOY_VERBS = {"redeploy", "deploy"}


def is_redeploy(text: str) -> bool:
    """Check if a message is a manual redeploy command."""
    word = text.strip().lower()
    return word in _REDEPLOY_ALIASES or word in _REDEPLOY_VERBS


# Timezone for scheduled tasks — uses system IANA timezone by default.
# TS equivalent: Intl.DateTimeFormat().resolvedOptions().timeZone
def _detect_timezone() -> str:
    if tz := os.environ.get("TZ"):
        return tz
    # Read /etc/localtime symlink → IANA name (works on Linux and macOS)
    try:
        link = os.readlink("/etc/localtime")
        parts = link.split("zoneinfo/")
        if len(parts) > 1:
            return parts[1]
    except OSError:
        pass
    return "UTC"


TIMEZONE: str = _detect_timezone()
