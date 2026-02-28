"""IPC handler for approval decision files.

When a decision file appears in approval_decisions/, this handler:
- Reads the decision and corresponding pending approval
- Executes the original request (if approved) or writes error (if denied)
- Writes the IPC response file so the container unblocks
- Cleans up pending and decision files

The policy check is skipped on execution since the human already approved.

Two handler types are supported:
- "service" (default): dispatches through plugin handlers (MCP service requests)
- "ipc": dispatches through ipc._registry.dispatch() with _cop_approved=True
  (host-mutating operations that went through cop_gate)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pynchy.config import get_settings
from pynchy.host.container_manager.ipc.handlers_service import _get_plugin_handlers
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.logger import logger
from pynchy.host.container_manager.security.audit import record_security_event


async def process_approval_decision(
    decision_file: Path, source_group: str, *, deps: Any = None
) -> None:
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
    handler_type = pending.get("handler_type", "service")

    # MCP proxy approvals: resolve the awaiting Future, don't execute here.
    # The proxy handler holds the HTTP connection open and handles execution.
    if handler_type == "mcp_proxy":
        from pynchy.host.container_manager.security.approval import resolve_mcp_proxy_approval

        resolved = resolve_mcp_proxy_approval(request_id, approved)
        if not resolved:
            logger.warning(
                "MCP proxy approval Future not found (timed out?)",
                request_id=request_id,
            )

        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="approved_by_user" if approved else "denied_by_user",
            request_id=request_id,
        )
        logger.info(
            "MCP proxy approval resolved",
            request_id=request_id,
            approved=approved,
        )

        pending_file.unlink(missing_ok=True)
        decision_file.unlink(missing_ok=True)
        return

    if approved:
        handler_type = pending.get("handler_type", "service")

        if handler_type == "ipc":
            await _execute_ipc_approval(request_data, source_group, request_id, deps)
        else:
            await _execute_service_approval(request_data, source_group, request_id, tool_name)

        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name=tool_name,
            decision="approved_by_user",
            request_id=request_id,
        )
    else:
        write_ipc_response(
            ipc_response_path(source_group, request_id),
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


async def _execute_service_approval(
    request_data: dict[str, Any],
    source_group: str,
    request_id: str,
    tool_name: str,
) -> None:
    """Dispatch an approved service request through plugin handlers."""
    handlers = _get_plugin_handlers()
    handler = handlers.get(tool_name)

    if handler is None:
        logger.warning("Approved tool no longer available", tool_name=tool_name)
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": f"Approved but tool '{tool_name}' is no longer available"},
        )
    else:
        try:
            request_data["source_group"] = source_group
            response = await handler(request_data)
            write_ipc_response(ipc_response_path(source_group, request_id), response)
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
                ipc_response_path(source_group, request_id),
                {"error": f"Execution failed: {exc}"},
            )


async def _execute_ipc_approval(
    request_data: dict[str, Any],
    source_group: str,
    request_id: str,
    deps: Any,
) -> None:
    """Dispatch an approved IPC request through the registry.

    Sets _cop_approved=True on the request data so the handler skips
    the cop_gate call on re-entry (prevents infinite approval loops).
    Admin-only: host-mutating ops already passed admin checks before
    cop_gate was invoked.
    """
    from pynchy.host.container_manager.ipc.registry import dispatch

    if deps is None:
        logger.error(
            "Cannot dispatch IPC approval without deps",
            request_id=request_id,
        )
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": "Internal error: IPC approval missing deps"},
        )
        return

    try:
        request_data["_cop_approved"] = True
        await dispatch(request_data, source_group, True, deps)
        # Note: the IPC handler writes its own response file on success.
        # We write one here only on failure to ensure the container unblocks.
        logger.info(
            "Approved IPC request dispatched",
            request_id=request_id,
            task_type=request_data.get("type"),
        )
    except Exception as exc:
        logger.error(
            "Approved IPC request failed",
            request_id=request_id,
            err=str(exc),
        )
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"error": f"Execution failed: {exc}"},
        )
