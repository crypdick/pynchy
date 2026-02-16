"""Shared IPC utilities for agent tools."""

from __future__ import annotations

import json
import os
import random
import time
from datetime import UTC, datetime
from pathlib import Path

IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
TASKS_DIR = IPC_DIR / "tasks"

# Context from environment variables (set by the agent runner)
chat_jid = os.environ.get("PYNCHY_CHAT_JID", "")
group_folder = os.environ.get("PYNCHY_GROUP_FOLDER", "")
is_god = os.environ.get("PYNCHY_IS_GOD") == "1"
is_scheduled_task = os.environ.get("PYNCHY_IS_SCHEDULED_TASK") == "1"


def write_ipc_file(directory: Path, data: dict) -> str:
    """Write an IPC file atomically (temp file + rename)."""
    directory.mkdir(parents=True, exist_ok=True)

    filename = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}.json"
    filepath = directory / filename

    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    return filename


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
