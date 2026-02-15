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


def is_context_reset(text: str) -> bool:
    """Check if a message is a context reset command."""
    words = text.strip().lower().split()
    if len(words) == 1:
        return words[0] in _RESET_ALIASES
    if len(words) == 2:
        a, b = words
        return (a in _RESET_VERBS and b in _RESET_NOUNS) or (
            a in _RESET_NOUNS and b in _RESET_VERBS
        )
    return False


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
