"""File-backed pending question state manager for the ask_user flow.

Manages pending question files in ipc/{group}/pending_questions/.
Each file represents a question the container is blocking on, waiting
for the user to answer via the channel (Slack, WhatsApp, etc.).

    container sends ask_user IPC request
        -> host writes pending_questions/{request_id}.json
        -> channel plugin posts interactive widget
        -> user answers via widget callback
        -> answer written as IPC response, pending file deleted

See docs/plans/2026-02-22-ask-user-blocking-design.md
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pynchy.config import get_settings
from pynchy.logger import logger

# How long before a pending question expires (seconds).
# Matches the container-side ASK_USER_TIMEOUT (1800s = 30 minutes).
PENDING_QUESTION_TIMEOUT_SECONDS = 1800

# -- Directory helpers ---------------------------------------------------------


def _pending_questions_dir(source_group: str) -> Path:
    """Return the pending_questions directory for a group, creating it if needed."""
    d = get_settings().data_dir / "ipc" / source_group / "pending_questions"
    d.mkdir(parents=True, exist_ok=True)
    return d


# -- State operations ----------------------------------------------------------


def create_pending_question(
    request_id: str,
    source_group: str,
    chat_jid: str,
    channel_name: str,
    session_id: str,
    questions: list[dict],
    message_id: str | None = None,
) -> None:
    """Write a pending question file atomically (tmp+rename).

    The file contains everything needed to deliver the answer back to the
    container and to post the interactive widget to the right channel.
    """
    pending_dir = _pending_questions_dir(source_group)

    data = {
        "request_id": request_id,
        "short_id": request_id[:8],
        "source_group": source_group,
        "chat_jid": chat_jid,
        "channel_name": channel_name,
        "session_id": session_id,
        "questions": questions,
        "message_id": message_id,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    filepath = pending_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    logger.info(
        "Pending question created",
        request_id=request_id,
        short_id=request_id[:8],
        source_group=source_group,
        channel_name=channel_name,
    )


def find_pending_question(request_id: str) -> dict | None:
    """Find a pending question by exact request_id, searching across all groups."""
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return None

    for group_dir in ipc_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name == "errors":
            continue
        filepath = group_dir / "pending_questions" / f"{request_id}.json"
        if filepath.exists():
            try:
                return json.loads(filepath.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return None


def find_pending_for_jid(chat_jid: str) -> dict | None:
    """Find a pending question by chat_jid, searching across all groups.

    Returns the first match (there should only be one pending question
    per chat at a time).

    Note: This performs a synchronous filesystem scan across all group IPC
    directories. Acceptable for personal deployments with a small number
    of groups and pending questions. If this becomes a bottleneck, consider
    an in-memory index keyed by chat_jid.
    """
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return None

    for group_dir in ipc_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name == "errors":
            continue
        pq_dir = group_dir / "pending_questions"
        if not pq_dir.exists():
            continue
        for filepath in pq_dir.glob("*.json"):
            try:
                data = json.loads(filepath.read_text())
                if data.get("chat_jid") == chat_jid:
                    return data
            except (json.JSONDecodeError, OSError):
                continue
    return None


def resolve_pending_question(request_id: str, source_group: str) -> None:
    """Delete the pending question file (question has been answered)."""
    pending_dir = _pending_questions_dir(source_group)
    filepath = pending_dir / f"{request_id}.json"
    if filepath.exists():
        filepath.unlink()
        logger.info(
            "Pending question resolved",
            request_id=request_id,
            source_group=source_group,
        )
    else:
        logger.warning(
            "Pending question file not found for resolve",
            request_id=request_id,
            source_group=source_group,
        )


def update_message_id(request_id: str, source_group: str, message_id: str) -> None:
    """Update the message_id field after the channel widget is posted.

    This lets the answer callback find the original message to update/remove it.
    Uses atomic write (tmp+rename) to avoid partial reads.
    """
    pending_dir = _pending_questions_dir(source_group)
    filepath = pending_dir / f"{request_id}.json"

    if not filepath.exists():
        logger.warning(
            "Pending question file not found for message_id update",
            request_id=request_id,
            source_group=source_group,
        )
        return

    try:
        data = json.loads(filepath.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "Failed to read pending question for message_id update",
            request_id=request_id,
            err=str(exc),
        )
        return

    data["message_id"] = message_id

    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    logger.info(
        "Pending question message_id updated",
        request_id=request_id,
        source_group=source_group,
        message_id=message_id,
    )


# -- Startup sweep -------------------------------------------------------------


async def sweep_expired_questions() -> list[dict]:
    """Find and auto-expire stale pending questions (crash recovery).

    Called on startup alongside ``sweep_expired_approvals()``.  Writes an
    error IPC response for each expired question so the container (if still
    alive) unblocks with a timeout error.

    Returns list of expired question dicts.
    """
    # Deferred import to avoid circular dependency:
    # pending_questions -> ipc._write -> ipc.__init__ -> ipc._handlers_ask_user -> pending_questions
    from pynchy.ipc._write import ipc_response_path, write_ipc_response

    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return []

    now = datetime.now(UTC)
    expired: list[dict] = []

    groups = [f.name for f in ipc_dir.iterdir() if f.is_dir() and f.name != "errors"]

    for grp in groups:
        pending_dir = ipc_dir / grp / "pending_questions"
        if not pending_dir.exists():
            continue
        for filepath in list(pending_dir.glob("*.json")):
            try:
                data = json.loads(filepath.read_text())
                ts = datetime.fromisoformat(data["timestamp"])
                age = (now - ts).total_seconds()

                if age > PENDING_QUESTION_TIMEOUT_SECONDS:
                    write_ipc_response(
                        ipc_response_path(grp, data["request_id"]),
                        {"error": "Question expired (no response within timeout)"},
                    )

                    filepath.unlink()
                    expired.append(data)

                    logger.info(
                        "Expired pending question auto-expired",
                        request_id=data["request_id"],
                        source_group=grp,
                        age_seconds=round(age),
                    )
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                logger.warning(
                    "Failed to process pending question during sweep",
                    path=str(filepath),
                    err=str(exc),
                )

    return expired
