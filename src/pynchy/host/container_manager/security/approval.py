"""File-backed approval state manager for the human approval gate.

Manages host-owned approval files outside the agent-mounted IPC tree.
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

import asyncio
import hashlib
import json
import os
import secrets
import string
from collections.abc import (  # noqa: TC003 - beartype resolves expiry callback annotations at runtime.
    Awaitable,
    Callable,
)
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - beartype resolves approval file paths at runtime.
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from pynchy.host.container_manager.ipc.write import (
    ipc_response_path,
    write_ipc_response,
    write_json_atomic,
)
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.identity import (
    guarded_action_id,
    request_payload_hash,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    OutboundEvent,
    OutboundEventType,
)
from pynchy.workspace.api import APPROVAL_TIMEOUT_SECONDS

# Alphabet for short approval IDs: lowercase + digits = 36 chars.
# 2-char IDs give 1296 combinations — more than enough for the handful
# of concurrent pending approvals in a personal assistant.
_SHORT_ID_ALPHABET = string.ascii_lowercase + string.digits

# ---------------------------------------------------------------------------
# MCP proxy approval futures -- awaited by the proxy HTTP handler
# ---------------------------------------------------------------------------

# Registry of in-flight MCP proxy approvals.  The proxy handler registers a
# Future before broadcasting the approval request; the IPC approval handler
# resolves it when the human responds.
_mcp_proxy_futures: dict[str, asyncio.Future[bool]] = {}
_approval_root: Path | None = None
_PAYLOAD_KEY_FILE = "approval-payload.key"
_ENCRYPTED_PAYLOAD_FIELD = "encrypted_payload"
_REDACTION_REQUIRED_FIELD = "redaction_required"
_REDACTION_REQUIRED = "required"
_REDACTION_NOT_REQUIRED = "not_required"


def configure_approval_state_root(path: Path) -> None:
    """Set host-only approval storage during application composition."""
    global _approval_root  # noqa: PLW0603 - one host process owns one approval root.
    _approval_root = path


def register_mcp_proxy_approval(request_id: str) -> asyncio.Future[bool]:
    """Register a Future for an MCP proxy approval request.

    The proxy handler awaits this Future while the HTTP connection is held
    open.  When the human approves/denies, resolve_mcp_proxy_approval()
    completes the Future and the proxy returns the response.
    """
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _mcp_proxy_futures[request_id] = fut
    return fut


def resolve_mcp_proxy_approval(request_id: str, *, approved: bool) -> bool:
    """Resolve a pending MCP proxy approval Future.

    Returns True if a matching Future was found and resolved, False otherwise.
    Called by process_approval_decision() when handler_type="mcp_proxy".
    """
    fut = _mcp_proxy_futures.pop(request_id, None)
    if fut is not None and not fut.done():
        fut.set_result(approved)
        return True
    return False


# Fields to omit from user-facing notification details
_INTERNAL_FIELDS = frozenset({"type", "request_id", "source_group"})

# Max characters for a detail value in notifications
_MAX_DETAIL_LEN = 100


# -- Directory helpers ---------------------------------------------------------


def _pending_approvals_dir(source_group: str) -> Path:
    """Return the pending_approvals directory for a group, creating it if needed."""
    d = approval_state_root() / source_group / "pending_approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _approval_decisions_dir(source_group: str) -> Path:
    """Return the approval_decisions directory for a group, creating it if needed."""
    d = approval_state_root() / source_group / "approval_decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def approval_state_root() -> Path:
    """Return approval state that is never mounted into an agent runtime."""
    if _approval_root is None:
        raise RuntimeError("approval state root has not been configured")
    return _approval_root


def _path_exists(path: Path) -> bool:
    return path.exists()


def _read_json_file(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _open_payload_key(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _payload_cipher(root: Path) -> Fernet:
    """Load or atomically create the host-only key for approval payloads."""
    key_path = root / _PAYLOAD_KEY_FILE
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        try:
            fd = _open_payload_key(key_path)
        except FileExistsError:
            key = key_path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as key_file:
                key_file.write(key)
    return Fernet(key)


def _decrypt_pending_payload(data: dict[str, Any], *, root: Path) -> dict[str, Any]:
    encrypted_payload = data.pop(_ENCRYPTED_PAYLOAD_FIELD, None)
    if encrypted_payload is None:
        raise ValueError("Pending approval encrypted payload is missing")
    if not isinstance(encrypted_payload, str):
        raise TypeError("Pending approval encrypted payload is invalid")
    try:
        payload = json.loads(_payload_cipher(root).decrypt(encrypted_payload.encode()))
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Pending approval encrypted payload cannot be read") from exc
    if not isinstance(payload, dict):
        raise TypeError("Pending approval encrypted payload is invalid")
    data.update(payload)
    return data


def _redaction_marker(*, secret_tainted: bool) -> str:
    """Encode security policy state without persisting a secret value."""
    if secret_tainted:
        return _REDACTION_REQUIRED
    return _REDACTION_NOT_REQUIRED


def _restore_secret_taint(data: dict[str, Any]) -> dict[str, Any]:
    """Populate replay-facing secret taint from durable redaction state."""
    marker = data.get(_REDACTION_REQUIRED_FIELD)
    if marker == _REDACTION_REQUIRED:
        data["secret_tainted"] = True
    elif marker == _REDACTION_NOT_REQUIRED:
        data["secret_tainted"] = False
    return data


def read_pending_approval(path: Path) -> dict[str, Any]:
    """Read a pending approval, decrypting its replay payload when present."""
    data = _read_json_file(path)
    if not isinstance(data, dict):
        raise TypeError("Pending approval is not an object")
    return _restore_secret_taint(_decrypt_pending_payload(data, root=path.parent.parent.parent))


def _pending_approval_files(pending_dir: Path) -> list[Path]:
    if not pending_dir.exists():
        return []
    return list(pending_dir.glob("*.json"))


def _unlink_path(path: Path) -> None:
    path.unlink()


# -- Short ID generation -------------------------------------------------------


def generate_short_id(source_group: str) -> str:
    """Generate a unique 2-char [a-z0-9] short ID for an approval request.

    Checks for collisions against existing pending approvals in the group.
    With 1296 possible IDs and typically 0-3 concurrent approvals, collisions
    are rare but handled gracefully.
    """
    pending_dir = _pending_approvals_dir(source_group)
    existing: set[str] = set()
    for filepath in pending_dir.glob("*.json"):
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            if "short_id" in data:
                existing.add(data["short_id"])
        except (json.JSONDecodeError, OSError):
            continue

    for _ in range(100):
        candidate = "".join(secrets.choice(_SHORT_ID_ALPHABET) for _ in range(2))
        if candidate not in existing:
            return candidate

    # Extremely unlikely — 1296 pending approvals in one group.
    # Fall back to 3-char ID.
    return "".join(secrets.choice(_SHORT_ID_ALPHABET) for _ in range(3))


# -- State operations ----------------------------------------------------------


def create_pending_approval(  # noqa: PLR0913 - approval files intentionally keep the request payload explicit.
    request_id: str,
    tool_name: str,
    source_group: str,
    approval_chat_jid: str,
    request_data: dict[str, Any],
    handler_type: str = "service",
    expires_after_seconds: int = APPROVAL_TIMEOUT_SECONDS,
    approval_scope: str = "exact_request",
    capability_id: str | None = None,
    origin_conversation_id: str | None = None,
    action_payload: dict[str, Any] | None = None,
    *,
    corruption_tainted: bool = False,
    secret_tainted: bool = False,
) -> str:
    """Write a pending approval file (PENDING state).

    The file contains everything needed to execute the request later,
    so the decision handler is self-contained.

    Args:
        handler_type: How to dispatch on approval — "service" routes through
            plugin handlers (existing MCP flow), "ipc" routes through
            ipc._registry.dispatch() (host-mutating cop_gate flow).

    Returns:
        The generated 2-char short_id for this approval request.
    """
    pending_dir = _pending_approvals_dir(source_group)
    short_id = generate_short_id(source_group)

    payload = {
        "request_data": request_data,
        "action_payload": action_payload,
    }
    data = {
        "request_id": request_id,
        "guarded_action_id": str(guarded_action_id(request_id)),
        "request_payload_hash": str(request_payload_hash(request_data)),
        "short_id": short_id,
        "tool_name": tool_name,
        "source_group": source_group,
        "approval_chat_jid": approval_chat_jid,
        "origin_conversation_id": origin_conversation_id,
        _ENCRYPTED_PAYLOAD_FIELD: _payload_cipher(approval_state_root())
        .encrypt(json.dumps(payload, sort_keys=True).encode())
        .decode(),
        "action_payload_sha256": (
            hashlib.sha256(json.dumps(action_payload, sort_keys=True).encode()).hexdigest()
            if action_payload is not None
            else None
        ),
        "handler_type": handler_type,
        "timestamp": datetime.now(UTC).isoformat(),
        "expires_after_seconds": expires_after_seconds,
        "approval_scope": approval_scope,
        "capability_id": capability_id,
        "allow_remember": capability_id is not None,
        # Persist request-time taint because the in-memory SecurityGate can be
        # gone when a host-owned approval decision is replayed after restart.
        "corruption_tainted": corruption_tainted,
        _REDACTION_REQUIRED_FIELD: _redaction_marker(secret_tainted=secret_tainted),
    }

    write_json_atomic(pending_dir / f"{request_id}.json", data, indent=2)

    logger.info(
        "Pending approval created",
        request_id=request_id,
        short_id=short_id,
        tool_name=tool_name,
        source_group=source_group,
    )

    return short_id


def list_pending_approvals(group: str | None = None) -> list[dict[str, Any]]:
    """List all pending approval files, optionally filtered by group.

    Returns parsed dicts sorted by timestamp (oldest first).
    """
    approval_dir = approval_state_root()

    if not approval_dir.exists():
        return []

    results: list[dict[str, Any]] = []

    groups = (
        [group]
        if group
        else [f.name for f in approval_dir.iterdir() if f.is_dir() and f.name != "errors"]
    )

    for grp in groups:
        pending_dir = approval_dir / grp / "pending_approvals"
        if not pending_dir.exists():
            continue
        for filepath in pending_dir.glob("*.json"):
            try:
                data = read_pending_approval(filepath)
                results.append(data)
            except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
                logger.warning(
                    "Failed to read pending approval",
                    path=str(filepath),
                    err=str(exc),
                )

    results.sort(key=lambda d: d.get("timestamp", ""))
    return results


def find_pending_by_short_id(short_id: str) -> dict[str, Any] | None:
    """Find a pending approval matching the given short ID.

    Scans file contents for the ``short_id`` field. With typically 0-3
    pending approvals across all groups, this is fast enough.
    """
    approval_dir = approval_state_root()
    if not approval_dir.exists():
        return None

    for group_dir in approval_dir.iterdir():
        if not group_dir.is_dir() or group_dir.name == "errors":
            continue
        pending_dir = group_dir / "pending_approvals"
        if not pending_dir.exists():
            continue
        for filepath in pending_dir.glob("*.json"):
            try:
                data = read_pending_approval(filepath)
                if data.get("short_id") == short_id:
                    return data
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                continue
    return None


async def sweep_expired_approvals(
    expire_action_intent: Callable[..., Awaitable[object]],
) -> list[dict[str, Any]]:
    """Find and auto-deny expired pending approvals. Clean orphaned decisions.

    Called on startup (crash recovery) and optionally on a slow timer.
    Returns list of expired approval dicts.
    """
    approval_dir = approval_state_root()
    if not await asyncio.to_thread(_path_exists, approval_dir):
        return []

    now = datetime.now(UTC)
    expired: list[dict[str, Any]] = []

    for group in await asyncio.to_thread(_approval_groups, approval_dir):
        pending_dir = approval_dir / group / "pending_approvals"
        decisions_dir = approval_dir / group / "approval_decisions"
        expired.extend(
            await _expire_pending_approvals(group, pending_dir, now, expire_action_intent)
        )
        pending_ids = await asyncio.to_thread(_pending_request_ids, pending_dir)
        await asyncio.to_thread(_remove_orphaned_decisions, decisions_dir, pending_ids)

    return expired


def _approval_groups(approval_dir: Path) -> list[str]:
    return [
        entry.name for entry in approval_dir.iterdir() if entry.is_dir() and entry.name != "errors"
    ]


async def _expire_pending_approvals(
    group: str,
    pending_dir: Path,
    now: datetime,
    expire_action_intent: Callable[..., Awaitable[object]],
) -> list[dict[str, Any]]:
    expired: list[dict[str, Any]] = []
    for filepath in await asyncio.to_thread(_pending_approval_files, pending_dir):
        expired_approval = await _expired_pending_approval(
            group, filepath, now, expire_action_intent
        )
        if expired_approval is not None:
            expired.append(expired_approval)
    return expired


async def _expired_pending_approval(
    group: str,
    filepath: Path,
    now: datetime,
    expire_action_intent: Callable[..., Awaitable[object]],
) -> dict[str, Any] | None:
    try:
        data = await asyncio.to_thread(read_pending_approval, filepath)
        timestamp = datetime.fromisoformat(data["timestamp"])
        age_seconds = (now - timestamp).total_seconds()
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Failed to process pending approval",
            path=str(filepath),
            err=str(exc),
        )
        return None

    raw_timeout = data.get("expires_after_seconds", APPROVAL_TIMEOUT_SECONDS)
    timeout = raw_timeout if isinstance(raw_timeout, int) and raw_timeout > 0 else 0
    if age_seconds <= timeout:
        return None

    await _auto_deny_expired_approval(group, filepath, data, age_seconds, expire_action_intent)
    return data


async def _auto_deny_expired_approval(
    group: str,
    filepath: Path,
    data: dict[str, Any],
    age_seconds: float,
    expire_action_intent: Callable[..., Awaitable[object]],
) -> None:
    await expire_action_intent(
        data["request_id"],
        reason="Approval expired before the external action was executed.",
    )
    await asyncio.to_thread(
        write_ipc_response,
        ipc_response_path(group, data["request_id"]),
        {"error": "Approval expired (no response within timeout)"},
    )
    await record_security_event(
        chat_jid=data.get("approval_chat_jid", "unknown"),
        workspace=group,
        tool_name=data.get("tool_name", "unknown"),
        decision="approval_expired",
        request_id=data["request_id"],
    )
    await asyncio.to_thread(_unlink_path, filepath)
    logger.info(
        "Expired pending approval auto-denied",
        request_id=data["request_id"],
        tool_name=data.get("tool_name"),
        age_seconds=round(age_seconds),
    )


def _pending_request_ids(pending_dir: Path) -> set[str]:
    if not pending_dir.exists():
        return set()
    return {filepath.stem for filepath in pending_dir.glob("*.json")}


def _remove_orphaned_decisions(decisions_dir: Path, pending_ids: set[str]) -> None:
    if not decisions_dir.exists():
        return
    for filepath in list(decisions_dir.glob("*.json")):
        if filepath.stem in pending_ids:
            continue
        logger.info("Removing orphaned decision file", path=str(filepath))
        filepath.unlink(missing_ok=True)


# -- Notification formatting ---------------------------------------------------


def format_approval_notification(
    tool_name: str,
    request_data: dict[str, Any],
    short_id: str,
    *,
    allow_remember: bool = False,
) -> str:
    """Format a user-facing approval notification message.

    Sanitizes request data: omits internal fields, truncates long values.
    """
    # NOTE: Update docs/usage/security.md "Approving a Request" when choices change.
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

    choices = (
        f"approve-once {short_id} / approve-session {short_id} / "
        f"approve-forever {short_id} / deny {short_id}"
        if allow_remember
        else f"approve {short_id}  /  deny {short_id}"
    )
    return (
        f"\U0001f510 Approval required\n"
        f"\n"
        f"Action: {tool_name}\n"
        f"Details:\n"
        f"{details_str}\n"
        f"\n"
        f"\u2192 {choices}"
    )


def approval_event(
    tool_name: str,
    request_data: dict[str, Any],
    short_id: str,
    *,
    preface: str | None = None,
    capability_id: str | None = None,
) -> OutboundEvent:
    """Build one channel-neutral approval prompt.

    Rich channels use the short ID to attach controls, while text-only
    channels render explicit command instructions in the notification body.
    Keeping this construction next to the pending-approval state prevents a
    gate producer from accidentally emitting an unstructured text message.
    """
    allow_remember = capability_id is not None
    content = format_approval_notification(
        tool_name,
        request_data,
        short_id,
        allow_remember=allow_remember,
    )
    if preface:
        content = f"{preface}\n\n{content}"
    return OutboundEvent(
        type=OutboundEventType.APPROVAL,
        content=content,
        metadata={
            "short_id": short_id,
            "tool_name": tool_name,
            **({"allow_remember": True} if allow_remember else {}),
        },
    )
