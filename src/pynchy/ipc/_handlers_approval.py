"""IPC handler for approval decision files.

When a decision file appears in approval_decisions/, this handler:
- Reads the decision and corresponding pending approval
- Executes the original request (if approved) or writes error (if denied)
- Writes the IPC response file so the container unblocks
- Cleans up pending and decision files

The policy check is skipped on execution since the human already approved.
"""

from __future__ import annotations

import json
from pathlib import Path

from pynchy.config import get_settings
from pynchy.ipc._handlers_service import _get_plugin_handlers
from pynchy.ipc._write import write_ipc_response
from pynchy.logger import logger
from pynchy.security.audit import record_security_event


def _response_path(source_group: str, request_id: str) -> Path:
    """Build the IPC response file path for a request."""
    return get_settings().data_dir / "ipc" / source_group / "responses" / f"{request_id}.json"


async def process_approval_decision(decision_file: Path, source_group: str) -> None:
    """Process an approval decision file — execute or deny the original request."""
    try:
        decision = json.loads(decision_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read decision file", path=str(decision_file), err=str(exc))
        decision_file.unlink(missing_ok=True)
        return

    request_id = decision.get("request_id")
    if not request_id:
        logger.warning("Decision file missing request_id", path=str(decision_file))
        decision_file.unlink(missing_ok=True)
        return

    # Find the corresponding pending approval
    s = get_settings()
    pending_file = s.data_dir / "ipc" / source_group / "pending_approvals" / f"{request_id}.json"

    if not pending_file.exists():
        logger.warning("No pending approval for decision", request_id=request_id)
        decision_file.unlink(missing_ok=True)
        return

    try:
        pending = json.loads(pending_file.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read pending file", path=str(pending_file), err=str(exc))
        decision_file.unlink(missing_ok=True)
        pending_file.unlink(missing_ok=True)
        return

    tool_name = pending.get("tool_name", "unknown")
    chat_jid = pending.get("chat_jid", "unknown")
    request_data = pending.get("request_data", {})
    approved = decision.get("approved", False)

    if approved:
        handlers = _get_plugin_handlers()
        handler = handlers.get(tool_name)

        if handler is None:
            logger.warning("Approved tool no longer available", tool_name=tool_name)
            write_ipc_response(
                _response_path(source_group, request_id),
                {"error": f"Approved but tool '{tool_name}' is no longer available"},
            )
        else:
            try:
                request_data["source_group"] = source_group
                response = await handler(request_data)
                write_ipc_response(_response_path(source_group, request_id), response)
                logger.info(
                    "Approved request executed",
                    request_id=request_id,
                    tool_name=tool_name,
                )
            except Exception as exc:
                logger.error(
                    "Approved request failed",
                    request_id=request_id,
                    err=str(exc),
                )
                write_ipc_response(
                    _response_path(source_group, request_id),
                    {"error": f"Execution failed: {exc}"},
                )

        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="approved_by_user",
            request_id=request_id,
        )
    else:
        write_ipc_response(
            _response_path(source_group, request_id),
            {"error": "Denied by user"},
        )
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="denied_by_user",
            request_id=request_id,
        )
        logger.info("Denied request", request_id=request_id, tool_name=tool_name)

    # Clean up files
    pending_file.unlink(missing_ok=True)
    decision_file.unlink(missing_ok=True)
