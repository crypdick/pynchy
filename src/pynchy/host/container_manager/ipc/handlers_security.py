"""IPC handlers for agent-tool security checks.

The cross-tool artifact hook reports file-capable operations before execution,
which establishes sticky workspace secret taint. Bash commands then run through
the taint-aware command cascade (blacklist -> Cop -> human approval).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_chat_jid
from pynchy.host.container_manager.ipc.handlers_artifact_security import (
    handle_artifact_security_check,
)
from pynchy.host.container_manager.ipc.registry import register_prefix
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.cop import (
    CopCommandDecision,
    CopCommandRisk,
    CopContextAvailability,
    CopInspectionContext,
    inspect_bash,
)

if TYPE_CHECKING:
    from pynchy.host.container_manager.ipc.handlers_artifact_security import _ArtifactSecurityDeps
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    get_gate_for_group,
)
from pynchy.logger import logger

# Inline network-capable check (same logic as container's classify.py).
# We duplicate rather than import because the container package isn't
# available on the host.
_NETWORK_SINGLE: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "nc",
        "netcat",
        "ncat",
        "telnet",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "nslookup",
        "dig",
        "host",
        "ping",
        "traceroute",
        "python",
        "python3",
        "node",
        "ruby",
        "perl",
        "php",
        "eval",
    }
)

_NETWORK_MULTI: tuple[str, ...] = (
    "apt-get install",
    "apt install",
    "pip install",
    "npm install",
    "yarn add",
    "cargo install",
    "bash -c",
    "sh -c",
)


def _allow() -> dict[str, str]:
    return {"decision": "allow"}


def _deny(reason: str) -> dict[str, str]:
    return {"decision": "deny", "reason": reason}


def _needs_human(reason: str) -> dict[str, str]:
    return {"decision": "needs_human", "reason": reason}


def _is_network_command(command: str) -> bool:
    """Check if command matches network-capable blacklist patterns."""
    cmd_lower = command.lower().strip()
    for pattern in _NETWORK_MULTI:
        if pattern in cmd_lower:
            return True
    first_token = cmd_lower.split()[0] if cmd_lower.split() else ""
    return first_token in _NETWORK_SINGLE


async def _cop_review(
    command: str,
    *,
    inspection_context: CopInspectionContext | None,
    risk: CopCommandRisk,
) -> dict[str, str]:
    verdict = await inspect_bash(command, inspection_context, risk)
    context_unavailable = (
        inspection_context is not None
        and inspection_context.availability is CopContextAvailability.UNAVAILABLE
    )
    if verdict.degraded or context_unavailable:
        return _needs_human("Cop or bounded action context unavailable")
    if verdict.decision is CopCommandDecision.APPROVE:
        return {**_allow(), "reason": verdict.reason, "reviewed_by": "cop"}
    if verdict.decision is CopCommandDecision.DENY:
        return {**_deny(verdict.reason), "reviewed_by": "cop"}
    return {**_needs_human(verdict.reason), "reviewed_by": "cop"}


async def evaluate_bash_command(
    gate: SecurityGate,
    command: str,
    inspection_context: CopInspectionContext | None = None,
) -> dict[str, str]:
    """Evaluate a bash command against taint state and classification.

    Three-tier cascade:
    1. No taint -> allow (no risk of compromised agent)
    2. Network blacklist hit -> escalate based on taint combo
    3. Grey zone (not blacklisted) -> Cop reviews, escalate if flagged

    Returns:
        {"decision": "allow"} or
        {"decision": "deny", "reason": "..."} or
        {"decision": "needs_human", "reason": "..."}
    """
    if not gate.cop_active:
        return _allow()

    # Tier 1: No taint -> allow unconditionally
    if not gate.corruption_tainted and not gate.secret_tainted:
        return _allow()

    # Tiers 2 and 3: the Cop sees the host-owned classification and taint facts.
    return await _cop_review(
        command,
        inspection_context=inspection_context,
        risk=CopCommandRisk(
            network_capable=_is_network_command(command),
            corruption_tainted=gate.corruption_tainted,
            secret_tainted=gate.secret_tainted,
        ),
    )


async def _handle_bash_security_check(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered prefix handler keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """IPC handler for security:bash_check requests.

    Receives bash commands from the container's BEFORE_TOOL_USE hook,
    evaluates them against the session's taint state, and writes back
    a decision (allow/deny/needs_human) via the IPC response file.
    """
    request_id = data.get("request_id")
    command = data.get("command", "")

    if not request_id:
        logger.warning("bash_check missing request_id", source_group=source_group)
        return

    gate = get_gate_for_group(source_group)
    if gate is None:
        del is_admin
        chat_jid = resolve_chat_jid(source_group, deps) or "unknown"
        reason = "No active security gate; Bash policy cannot be evaluated"
        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name="Bash",
            decision="bash_gate_unavailable",
            reason=reason,
            request_id=request_id,
        )
        write_ipc_response(
            ipc_response_path(source_group, request_id),
            {"result": {**_deny(reason), "guarded_action_id": request_id}},
        )
        return

    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"
    inspection_context = await deps.load_cop_inspection_context(chat_jid)
    decision = await evaluate_bash_command(gate, command, inspection_context)

    if decision["decision"] == "needs_human":
        # Lazy import to avoid circular: security.approval -> ipc._write -> ipc.__init__ -> here
        from pynchy.host.container_manager.security.approval import (  # noqa: PLC0415 - avoid security.approval/import cycle.
            approval_event,
            create_pending_approval,
        )

        short_id = create_pending_approval(
            request_id=request_id,
            tool_name="Bash",
            source_group=source_group,
            approval_chat_jid=chat_jid,
            request_data={"command": command},
            handler_type="security_bash",
            corruption_tainted=gate.corruption_tainted,
            secret_tainted=gate.secret_tainted,
        )

        await deps.broadcast_to_channels(
            chat_jid,
            approval_event(
                "Bash",
                {"command": command},
                short_id,
                preface=f"[Cop escalated: {decision.get('reason', 'uncertain command')}]",
            ),
        )

        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name="Bash",
            decision="approval_requested",
            corruption_tainted=gate.corruption_tainted,
            secret_tainted=gate.secret_tainted,
            reason=decision.get("reason"),
            request_id=request_id,
        )
        # No response file — container blocks until human approves/denies
        return

    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name="Bash",
        decision=(
            f"cop_{'approved' if decision['decision'] == 'allow' else 'denied'}"
            if decision.get("reviewed_by") == "cop"
            else decision["decision"]
        ),
        corruption_tainted=gate.corruption_tainted,
        secret_tainted=gate.secret_tainted,
        reason=decision.get("reason"),
        request_id=request_id,
    )

    response_path = ipc_response_path(source_group, request_id)
    wire_decision = {key: value for key, value in decision.items() if key != "reviewed_by"}
    # Agent-side request/response IPC unwraps the public ``result`` field.
    write_ipc_response(
        response_path,
        {"result": {**wire_decision, "guarded_action_id": request_id}},
    )


async def _handle_security_check(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered prefix handler keeps the IPC dispatch contract.
    deps: IpcDeps,
) -> None:
    """Dispatch a typed security request without tool-name assumptions."""
    if data.get("type") == "security:artifact_check":
        await handle_artifact_security_check(
            data, source_group, is_admin, cast("_ArtifactSecurityDeps", deps)
        )
        return
    await _handle_bash_security_check(data, source_group, is_admin, deps)


# Register the prefix handler so all "security:*" IPC types route here.
register_prefix("security:", _handle_security_check)
