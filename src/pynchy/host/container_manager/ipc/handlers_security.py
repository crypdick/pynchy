"""IPC handler for bash security checks.

Evaluates bash commands against taint state and the three-tier cascade
(blacklist -> Cop -> human approval). Called by the container's
BEFORE_TOOL_USE hook via IPC.
"""

from __future__ import annotations

from typing import Any

from pynchy.host.container_manager.ipc.deps import IpcDeps, resolve_chat_jid
from pynchy.host.container_manager.ipc.registry import register_prefix
from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.logger import logger
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.cop import inspect_bash
from pynchy.host.container_manager.security.gate import SecurityGate, get_gate_for_group, resolve_security

# Inline network-capable check (same logic as container's classify.py).
# We duplicate rather than import because the container package isn't
# available on the host.
_NETWORK_SINGLE: frozenset[str] = frozenset({
    "curl", "wget", "nc", "netcat", "ncat", "telnet",
    "ssh", "scp", "sftp", "rsync",
    "nslookup", "dig", "host", "ping", "traceroute",
    "python", "python3", "node", "ruby", "perl", "php",
    "eval",
})

_NETWORK_MULTI: tuple[str, ...] = (
    "apt-get install", "apt install",
    "pip install", "npm install", "yarn add", "cargo install",
    "bash -c", "sh -c",
)


def _is_network_command(command: str) -> bool:
    """Check if command matches network-capable blacklist patterns."""
    cmd_lower = command.lower().strip()
    for pattern in _NETWORK_MULTI:
        if pattern in cmd_lower:
            return True
    first_token = cmd_lower.split()[0] if cmd_lower.split() else ""
    return first_token in _NETWORK_SINGLE


async def evaluate_bash_command(gate: SecurityGate, command: str) -> dict:
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
    policy = gate.policy

    # Tier 1: No taint -> allow unconditionally
    if not policy.corruption_tainted and not policy.secret_tainted:
        return {"decision": "allow"}

    both_tainted = policy.corruption_tainted and policy.secret_tainted

    # Tier 2: Network blacklist
    if _is_network_command(command):
        if both_tainted:
            # Lethal trifecta: corruption + secret + network -> human
            return {
                "decision": "needs_human",
                "reason": f"Network command while corruption+secret tainted: {command[:200]}",
            }
        # Single taint (corruption only) + network -> Cop review
        verdict = await inspect_bash(command)
        if verdict.flagged:
            return {"decision": "deny", "reason": verdict.reason or "Cop flagged command"}
        return {"decision": "allow"}

    # Tier 3: Grey zone -> Cop review
    verdict = await inspect_bash(command)
    if verdict.flagged:
        if both_tainted:
            return {
                "decision": "needs_human",
                "reason": verdict.reason or "Cop flagged command",
            }
        return {"decision": "deny", "reason": verdict.reason or "Cop flagged command"}

    return {"decision": "allow"}


async def _handle_bash_security_check(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,
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
        security = resolve_security(source_group, is_admin=is_admin)
        gate = SecurityGate(security)

    chat_jid = resolve_chat_jid(source_group, deps) or "unknown"

    decision = await evaluate_bash_command(gate, command)

    if decision["decision"] == "needs_human":
        # Lazy import to avoid circular: security.approval -> ipc._write -> ipc.__init__ -> here
        from pynchy.host.container_manager.security.approval import create_pending_approval, format_approval_notification

        short_id = create_pending_approval(
            request_id=request_id,
            tool_name="Bash",
            source_group=source_group,
            chat_jid=chat_jid,
            request_data={"command": command},
        )
        notification = format_approval_notification("Bash", {"command": command}, short_id)
        await deps.broadcast_to_channels(chat_jid, notification)

        await record_security_event(
            chat_jid=chat_jid,
            workspace=source_group,
            tool_name="Bash",
            decision="approval_requested",
            corruption_tainted=gate.policy.corruption_tainted,
            secret_tainted=gate.policy.secret_tainted,
            reason=decision.get("reason"),
            request_id=request_id,
        )
        # No response file — container blocks until human approves/denies
        return

    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name="Bash",
        decision=decision["decision"],
        corruption_tainted=gate.policy.corruption_tainted,
        secret_tainted=gate.policy.secret_tainted,
        reason=decision.get("reason"),
        request_id=request_id,
    )

    response_path = ipc_response_path(source_group, request_id)
    write_ipc_response(response_path, decision)


# Register the prefix handler so all "security:*" IPC types route here.
register_prefix("security:", _handle_bash_security_check)
