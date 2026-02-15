"""Shared utility functions.

Small helpers used across multiple modules. Avoids duplication of common
patterns like timestamped ID generation, safe JSON loading, and schedule
calculations.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from croniter import croniter

from pynchy.logger import logger


def generate_message_id(prefix: str = "") -> str:
    """Generate a unique message ID using millisecond timestamp.

    Args:
        prefix: Optional prefix (e.g. "host", "tui", "sys-notice").
                When provided, the ID is ``{prefix}-{ms_timestamp}``.
                When empty, returns just the ms timestamp string.
    """
    ms = int(datetime.now(UTC).timestamp() * 1000)
    return f"{prefix}-{ms}" if prefix else str(ms)


def safe_json_load(path: Path, *, default: object = None) -> object:
    """Read and parse a JSON file, returning *default* on any error.

    Logs a warning on failure so callers don't need their own try/except.
    """
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read JSON file", path=str(path), err=str(exc))
        return default


def compute_next_run(
    schedule_type: Literal["cron", "interval", "once"],
    schedule_value: str,
    timezone: str,
) -> str | None:
    """Compute the next run ISO timestamp for a scheduled task.

    Returns None for 'once' tasks (no recurrence) or if the input is invalid.
    Raises ValueError for invalid cron/interval values so callers can reject them.
    """
    if schedule_type == "cron":
        tz = ZoneInfo(timezone)
        cron = croniter(schedule_value, datetime.now(tz))
        return cron.get_next(datetime).isoformat()

    if schedule_type == "interval":
        ms = int(schedule_value)
        if ms <= 0:
            raise ValueError("Interval must be positive")
        return datetime.fromtimestamp(
            datetime.now(UTC).timestamp() + ms / 1000,
            tz=UTC,
        ).isoformat()

    # 'once' tasks: no next run after execution
    return None
