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
from collections.abc import (
    Callable,  # beartype resolves recovery callback annotations.
)
from datetime import UTC, datetime
from pathlib import (
    Path,
)
from typing import Any, cast

from pynchy.atomic_json import write_json_atomic
from pynchy.identifiers import validate_request_id
from pynchy.logger import logger

# How long before a pending question expires (seconds).
# Matches the container-side ASK_USER_TIMEOUT (1800s = 30 minutes).
PENDING_QUESTION_TIMEOUT_SECONDS = 1800
_EXPIRATION_ERROR = "Question expired (no response within timeout)"
type ExpirationResponseWriter = Callable[[str, str, str], None]

_ipc_base_dir: Path | None = None


def configure_pending_questions_ipc_base_dir(path: Path) -> None:
    """Set the host-owned IPC root during application composition."""
    global _ipc_base_dir  # noqa: PLW0603 - one host process owns one pending-question IPC root.
    _ipc_base_dir = path


def _configured_ipc_base_dir() -> Path:
    if _ipc_base_dir is None:
        raise RuntimeError("pending-question IPC base directory has not been configured")
    return _ipc_base_dir


# -- Directory helpers ---------------------------------------------------------


def _pending_questions_dir(source_group: str) -> Path:
    """Return the pending_questions directory for a group, creating it if needed."""
    d = _configured_ipc_base_dir() / source_group / "pending_questions"
    d.mkdir(parents=True, exist_ok=True)
    return d


# -- State operations ----------------------------------------------------------


def create_pending_question(  # noqa: PLR0913 - file-backed pending-question payload mirrors the channel callback contract.
    request_id: str,
    source_group: str,
    chat_jid: str,
    channel_name: str,
    session_id: str,
    questions: list[dict[str, Any]],
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

    safe_request_id = validate_request_id(request_id)
    filepath = pending_dir / f"{safe_request_id}.json"
    write_json_atomic(filepath, data, indent=2)

    logger.info(
        "Pending question created",
        request_id=request_id,
        short_id=request_id[:8],
        source_group=source_group,
        channel_name=channel_name,
    )


def find_pending_question(request_id: str) -> dict[str, Any] | None:
    """Find a pending question by exact request_id, searching across all groups."""
    try:
        safe_request_id = validate_request_id(request_id)
    except ValueError:
        return None
    ipc_dir = _configured_ipc_base_dir()
    if not ipc_dir.exists():
        return None

    for group_dir in ipc_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name == "errors":
            continue
        filepath = group_dir / "pending_questions" / f"{safe_request_id}.json"
        if filepath.exists():
            try:
                return cast("dict[str, Any]", json.loads(filepath.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
    return None


def find_pending_for_jid(chat_jid: str) -> dict[str, Any] | None:
    """Find a pending question by chat_jid, searching across all groups.

    Returns the first match (there should only be one pending question
    per chat at a time).

    Note: This performs a synchronous filesystem scan across all group IPC
    directories. Acceptable for personal deployments with a small number
    of groups and pending questions. If this becomes a bottleneck, consider
    an in-memory index keyed by chat_jid.
    """
    ipc_dir = _configured_ipc_base_dir()
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
                data = cast("dict[str, Any]", json.loads(filepath.read_text()))
                if data.get("chat_jid") == chat_jid:
                    return data
            except (json.JSONDecodeError, OSError):
                continue
    return None


def resolve_pending_question(request_id: str, source_group: str) -> None:
    """Delete the pending question file once the question is answered."""
    pending_dir = _pending_questions_dir(source_group)
    safe_request_id = validate_request_id(request_id)
    filepath = pending_dir / f"{safe_request_id}.json"
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

    This lets the answer callback find the posted message to update/remove it.
    Uses atomic write (tmp+rename) to avoid partial reads.
    """
    pending_dir = _pending_questions_dir(source_group)
    safe_request_id = validate_request_id(request_id)
    filepath = pending_dir / f"{safe_request_id}.json"

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

    write_json_atomic(filepath, data, indent=2)

    logger.info(
        "Pending question message_id updated",
        request_id=request_id,
        source_group=source_group,
        message_id=message_id,
    )


# -- Startup sweep -------------------------------------------------------------


async def sweep_expired_questions(  # noqa: RUF029 - IPC startup awaits the recovery sweep API.
    write_expiration_response: ExpirationResponseWriter,
) -> list[dict[str, Any]]:
    """Find and auto-expire stale pending questions (crash recovery).

    Called on startup alongside ``sweep_expired_approvals()``.  Writes an
    error IPC response for each expired question so the container (if still
    alive) unblocks with a timeout error.

    Returns list of expired question dicts.
    """
    ipc_dir = _configured_ipc_base_dir()
    if not ipc_dir.exists():
        return []

    now = datetime.now(UTC)
    expired: list[dict[str, Any]] = []

    groups = [f.name for f in ipc_dir.iterdir() if f.is_dir() and f.name != "errors"]

    for grp in groups:
        pending_dir = ipc_dir / grp / "pending_questions"
        if not pending_dir.exists():
            continue
        for filepath in list(pending_dir.glob("*.json")):
            try:
                data = json.loads(filepath.read_text())
                ts = datetime.fromisoformat(data["timestamp"])
            except (json.JSONDecodeError, OSError, KeyError) as exc:
                logger.warning(
                    "Failed to process pending question during sweep",
                    path=str(filepath),
                    err=str(exc),
                )
                continue

            age = (now - ts).total_seconds()
            if age <= PENDING_QUESTION_TIMEOUT_SECONDS:
                continue
            try:
                _expire_pending_question(grp, filepath, data, age, write_expiration_response)
            except (OSError, KeyError) as exc:
                logger.warning(
                    "Failed to process pending question during sweep",
                    path=str(filepath),
                    err=str(exc),
                )
                continue
            expired.append(data)

    return expired


def _expire_pending_question(  # expiry context is preserved for recovery audit logging.
    group_name: str,
    filepath: Path,
    data: dict[str, Any],
    age_seconds: float,
    write_expiration_response: ExpirationResponseWriter,
) -> None:
    write_expiration_response(group_name, data["request_id"], _EXPIRATION_ERROR)

    filepath.unlink()

    logger.info(
        "Expired pending question auto-expired",
        request_id=data["request_id"],
        source_group=group_name,
        age_seconds=round(age_seconds),
    )
