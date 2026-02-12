"""Configuration constants and paths.

Port of src/config.ts — all time intervals in seconds, all paths as pathlib.Path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ASSISTANT_NAME: str = os.environ.get("ASSISTANT_NAME", "Andy")

# Intervals in seconds (TS uses milliseconds)
POLL_INTERVAL: float = 2.0
SCHEDULER_POLL_INTERVAL: float = 60.0
IPC_POLL_INTERVAL: float = 1.0

# Paths
PROJECT_ROOT: Path = Path.cwd()
HOME_DIR: Path = Path(os.environ.get("HOME", "/Users/user"))

MOUNT_ALLOWLIST_PATH: Path = HOME_DIR / ".config" / "nanoclaw" / "mount-allowlist.json"
STORE_DIR: Path = (PROJECT_ROOT / "store").resolve()
GROUPS_DIR: Path = (PROJECT_ROOT / "groups").resolve()
DATA_DIR: Path = (PROJECT_ROOT / "data").resolve()
MAIN_GROUP_FOLDER: str = "main"

# Container settings (time values converted from ms to seconds)
CONTAINER_IMAGE: str = os.environ.get("CONTAINER_IMAGE", "nanoclaw-agent:latest")
CONTAINER_TIMEOUT: float = int(os.environ.get("CONTAINER_TIMEOUT", "1800000")) / 1000
CONTAINER_MAX_OUTPUT_SIZE: int = int(
    os.environ.get("CONTAINER_MAX_OUTPUT_SIZE", "10485760")
)  # 10MB
IDLE_TIMEOUT: float = int(os.environ.get("IDLE_TIMEOUT", "1800000")) / 1000  # 30min
MAX_CONCURRENT_CONTAINERS: int = max(1, int(os.environ.get("MAX_CONCURRENT_CONTAINERS", "5")))


def _escape_regex(s: str) -> str:
    return re.escape(s)


TRIGGER_PATTERN: re.Pattern[str] = re.compile(
    rf"^@{_escape_regex(ASSISTANT_NAME)}\b", re.IGNORECASE
)


# Timezone for scheduled tasks — uses system timezone by default
def _detect_timezone() -> str:
    if tz := os.environ.get("TZ"):
        return tz
    try:
        import time

        return time.tzname[0] or "UTC"
    except Exception:
        return "UTC"


TIMEZONE: str = _detect_timezone()
