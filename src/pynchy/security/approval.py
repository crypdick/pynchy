"""File-backed approval state manager for the human approval gate.

Manages pending approval files in ipc/{group}/pending_approvals/.
Each file represents a PENDING state in the approval state machine:

    request arrives (needs_human=True)
        → write pending_approvals/{request_id}.json
        → broadcast notification to chat
        → container blocks (no response file written)

    user sends "approve <id>" or "deny <id>"
        → write approval_decisions/{request_id}.json
        → watcher picks up decision, executes or denies, writes response

    startup sweep: auto-deny stale pending files, clean orphaned decisions

See docs/plans/2026-02-24-human-approval-gate-design.md
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pynchy.config import get_settings
from pynchy.ipc._write import write_ipc_response
from pynchy.logger import logger
from pynchy.security.audit import record_security_event

# How long before a pending approval expires (seconds).
# Matches the container-side IPC response poll timeout (300s).
APPROVAL_TIMEOUT_SECONDS = 300

# Fields to omit from user-facing notification details
_INTERNAL_FIELDS = frozenset({"type", "request_id", "source_group"})

# Max characters for a detail value in notifications
_MAX_DETAIL_LEN = 100


# -- Directory helpers ---------------------------------------------------------


def _pending_approvals_dir(source_group: str) -> Path:
    """Return the pending_approvals directory for a group, creating it if needed."""
    d = get_settings().data_dir / "ipc" / source_group / "pending_approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _approval_decisions_dir(source_group: str) -> Path:
    """Return the approval_decisions directory for a group, creating it if needed."""
    d = get_settings().data_dir / "ipc" / source_group / "approval_decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _response_path(source_group: str, request_id: str) -> Path:
    """Build the IPC response file path for a request."""
    return get_settings().data_dir / "ipc" / source_group / "responses" / f"{request_id}.json"


# -- State operations ----------------------------------------------------------


def create_pending_approval(
    request_id: str,
    tool_name: str,
    source_group: str,
    chat_jid: str,
    request_data: dict,
) -> None:
    """Write a pending approval file (PENDING state).

    The file contains everything needed to execute the request later,
    so the decision handler is self-contained.
    """
    pending_dir = _pending_approvals_dir(source_group)

    data = {
        "request_id": request_id,
        "short_id": request_id[:8],
        "tool_name": tool_name,
        "source_group": source_group,
        "chat_jid": chat_jid,
        "request_data": request_data,
        "timestamp": datetime.now(UTC).isoformat(),
    }

    filepath = pending_dir / f"{request_id}.json"
    temp_path = filepath.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(data, indent=2))
    temp_path.rename(filepath)

    logger.info(
        "Pending approval created",
        request_id=request_id,
        short_id=request_id[:8],
        tool_name=tool_name,
        source_group=source_group,
    )


def list_pending_approvals(group: str | None = None) -> list[dict]:
    """List all pending approval files, optionally filtered by group.

    Returns parsed dicts sorted by timestamp (oldest first).
    """
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"

    if not ipc_dir.exists():
        return []

    results: list[dict] = []

    groups = (
        [group]
        if group
        else [f.name for f in ipc_dir.iterdir() if f.is_dir() and f.name != "errors"]
    )

    for grp in groups:
        pending_dir = ipc_dir / grp / "pending_approvals"
        if not pending_dir.exists():
            continue
        for filepath in pending_dir.glob("*.json"):
            try:
                data = json.loads(filepath.read_text())
                results.append(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "Failed to read pending approval",
                    path=str(filepath),
                    err=str(exc),
                )

    results.sort(key=lambda d: d.get("timestamp", ""))
    return results


def find_pending_by_short_id(short_id: str) -> dict | None:
    """Find a pending approval matching the given short ID prefix."""
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return None

    for group_dir in ipc_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name == "errors":
            continue
        pending_dir = group_dir / "pending_approvals"
        if not pending_dir.exists():
            continue
        for filepath in pending_dir.glob(f"{short_id}*.json"):
            try:
                return json.loads(filepath.read_text())
            except (json.JSONDecodeError, OSError):
                continue
    return None


async def sweep_expired_approvals() -> list[dict]:
    """Find and auto-deny expired pending approvals. Clean orphaned decisions.

    Called on startup (crash recovery) and optionally on a slow timer.
    Returns list of expired approval dicts.
    """
    s = get_settings()
    ipc_dir = s.data_dir / "ipc"
    if not ipc_dir.exists():
        return []

    now = datetime.now(UTC)
    expired: list[dict] = []

    groups = [f.name for f in ipc_dir.iterdir() if f.is_dir() and f.name != "errors"]

    for grp in groups:
        pending_dir = ipc_dir / grp / "pending_approvals"
        decisions_dir = ipc_dir / grp / "approval_decisions"

        # Sweep expired pending approvals
        if pending_dir.exists():
            for filepath in list(pending_dir.glob("*.json")):
                try:
                    data = json.loads(filepath.read_text())
                    ts = datetime.fromisoformat(data["timestamp"])
                    age = (now - ts).total_seconds()

                    if age > APPROVAL_TIMEOUT_SECONDS:
                        # Auto-deny: write error response
                        write_ipc_response(
                            _response_path(grp, data["request_id"]),
                            {"error": "Approval expired (no response within timeout)"},
                        )

                        await record_security_event(
                            chat_jid=data.get("chat_jid", "unknown"),
                            workspace=grp,
                            tool_name=data.get("tool_name", "unknown"),
                            decision="approval_expired",
                            request_id=data["request_id"],
                        )

                        filepath.unlink()
                        expired.append(data)

                        logger.info(
                            "Expired pending approval auto-denied",
                            request_id=data["request_id"],
                            tool_name=data.get("tool_name"),
                            age_seconds=round(age),
                        )
                except (json.JSONDecodeError, OSError, KeyError) as exc:
                    logger.warning(
                        "Failed to process pending approval",
                        path=str(filepath),
                        err=str(exc),
                    )

        # Clean orphaned decision files (decision exists but no matching pending)
        if decisions_dir.exists():
            pending_ids = set()
            if pending_dir.exists():
                pending_ids = {f.stem for f in pending_dir.glob("*.json")}

            for filepath in list(decisions_dir.glob("*.json")):
                if filepath.stem not in pending_ids:
                    logger.info("Removing orphaned decision file", path=str(filepath))
                    filepath.unlink(missing_ok=True)

    return expired


# -- Notification formatting ---------------------------------------------------


def format_approval_notification(
    tool_name: str,
    request_data: dict,
    short_id: str,
) -> str:
    """Format a user-facing approval notification message.

    Sanitizes request data: omits internal fields, truncates long values.
    """
    details = {
        k: v for k, v in request_data.items() if k not in _INTERNAL_FIELDS and not k.startswith("_")
    }

    detail_parts: list[str] = []
    for key, value in details.items():
        s = str(value)
        if len(s) > _MAX_DETAIL_LEN:
            s = s[:_MAX_DETAIL_LEN] + "..."
        detail_parts.append(f"  {key}: {s}")

    details_str = "\n".join(detail_parts) if detail_parts else "  (no details)"

    return (
        f"\U0001f510 Approval required\n"
        f"\n"
        f"Action: {tool_name}\n"
        f"Details:\n"
        f"{details_str}\n"
        f"\n"
        f"\u2192 approve {short_id}  /  deny {short_id}"
    )
