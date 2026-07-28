"""In-container bash security hook.

Runs as a BEFORE_TOOL_USE hook. Classifies the command locally:
- SAFE (whitelist) -> allow without IPC
- NETWORK/UNKNOWN -> IPC to host for taint check + Cop

The host returns allow/deny/needs_human. Human approval blocks
the IPC response until the user approves or the request times out.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agent_runner.hooks import HookDecision
from agent_runner.security.action_identity import current_guarded_action_id
from agent_runner.security.classify import CommandClass, classify_command


def _log(message: str) -> None:
    sys.stderr.write(f"[bash-gate] {message}\n")
    sys.stderr.flush()


async def _ipc_bash_check(command: str) -> HookDecision:
    """Send a bash security check to the host via IPC and wait for response.

    Reuses the existing ipc_service_request machinery (watchdog-based).
    """
    from agent_runner.agent_tools._ipc_request import (  # noqa: PLC0415 - defer IPC machinery until host escalation is required.
        ipc_service_request,
    )

    try:
        results = await ipc_service_request(
            "bash_check",
            {"command": command},
            response_timeout_seconds=300,  # Match approval timeout
            type_override="security:bash_check",
            guarded_action_id=str(current_guarded_action_id()),
        )
    except Exception as exc:  # allow: exception-handling  # noqa: BLE001
        reason = f"Host Bash policy unavailable; failing closed: {type(exc).__name__}"
        _log(reason)
        return HookDecision(allowed=False, reason=reason)

    return _parse_host_decision(results)


def _parse_host_decision(results: list[Any]) -> HookDecision:
    """Parse the host result without treating absence or novelty as approval."""
    denial_reason: str | None = None

    if not results:
        denial_reason = "Host returned an empty Bash decision; failing closed"
    else:
        text = results[0].text
        if text.startswith("Error:"):
            denial_reason = "Host Bash policy unavailable; failing closed"
        else:
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                data = None
            if not isinstance(data, dict):
                denial_reason = "Host returned a malformed Bash decision; failing closed"
            elif data.get("decision") == "allow":
                return HookDecision(allowed=True)
            elif data.get("decision") == "deny":
                host_reason = data.get("reason")
                reason = host_reason if isinstance(host_reason, str) else "Denied by host policy"
                return HookDecision(allowed=False, reason=reason)
            else:
                denial_reason = "Host returned an unknown Bash decision; failing closed"

    _log(denial_reason)
    return HookDecision(allowed=False, reason=denial_reason)


async def bash_security_hook(tool_name: str, tool_input: dict[str, Any]) -> HookDecision:
    """BEFORE_TOOL_USE hook for bash command security gating.

    Only gates the "Bash" tool. All other tools pass through.
    """
    if tool_name != "Bash":
        return HookDecision(allowed=True)

    command = tool_input.get("command", "")
    if not command.strip():
        return HookDecision(allowed=True)

    # Tier 2: Whitelist -- provably local, no IPC needed
    classification = classify_command(command)
    if classification == CommandClass.SAFE:
        return HookDecision(allowed=True)

    # Tiers 1/3/4: Require host evaluation (taint state lives there)
    _log(f"Escalating to host: {classification.value} — {command[:100]}")
    return await _ipc_bash_check(command)
