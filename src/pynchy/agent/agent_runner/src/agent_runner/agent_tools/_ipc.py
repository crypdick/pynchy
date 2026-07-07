"""Shared IPC utilities for agent tools."""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

IPC_SCHEMA_VERSION = 1
IPC_DIR = Path("/workspace/ipc")
MESSAGES_DIR = IPC_DIR / "messages"
REQUESTS_DIR = IPC_DIR / "requests"

# Context from environment variables (set by the agent runner)
chat_jid = os.environ.get("PYNCHY_CHAT_JID", "")
group_folder = os.environ.get("PYNCHY_GROUP_FOLDER", "")
is_admin = os.environ.get("PYNCHY_IS_ADMIN") == "1"
is_scheduled_task = os.environ.get("PYNCHY_IS_SCHEDULED_TASK") == "1"


def write_ipc_file(directory: Path, data: dict[str, Any]) -> str:
    """Write an IPC file atomically (temp file + rename)."""
    directory.mkdir(parents=True, exist_ok=True)

    filename = f"{int(time.time() * 1000)}-{random.randbytes(3).hex()}.json"
    filepath = directory / filename

    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    return filename


def make_ipc_request(
    kind: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    reply_to: str | None = "responses",
    deadline: str | None = None,
) -> dict[str, Any]:
    """Build a canonical IPC request envelope for host-bound operations."""
    return {
        "schema_version": IPC_SCHEMA_VERSION,
        "kind": kind,
        "request_id": request_id or uuid.uuid4().hex,
        "source_group": group_folder,
        "created_at": now_iso(),
        "reply_to": reply_to,
        "deadline": deadline,
        "payload": payload,
    }


def write_request_file(
    kind: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    reply_to: str | None = "responses",
    deadline: str | None = None,
) -> tuple[str, str]:
    """Write a canonical request file and return (filename, request_id)."""
    envelope = make_ipc_request(
        kind,
        payload,
        request_id=request_id,
        reply_to=reply_to,
        deadline=deadline,
    )
    filename = write_ipc_file(REQUESTS_DIR, envelope)
    return filename, envelope["request_id"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
