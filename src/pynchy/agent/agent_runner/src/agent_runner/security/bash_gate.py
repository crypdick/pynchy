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

from agent_runner.hooks import HookDecision
from agent_runner.security.classify import CommandClass, classify_command


def _log(message: str) -> None:
    print(f"[bash-gate] {message}", file=sys.stderr, flush=True)


async def _ipc_bash_check(command: str) -> HookDecision:
    """Send a bash security check to the host via IPC and wait for response.

    Reuses the existing ipc_service_request machinery (watchdog-based).
    """
    from agent_runner.agent_tools._ipc_request import ipc_service_request

    results = await ipc_service_request(
        "bash_check",
        {"command": command},
        timeout=300,  # Match approval timeout
        type_override="security:bash_check",
    )

    # Parse the response
    if not results:
        _log("Empty IPC response, allowing command")
        return HookDecision(allowed=True)

    text = results[0].text
    if text.startswith("Error:"):
        # IPC error (timeout, etc.) -- fail open with warning
        _log(f"IPC error: {text}")
        return HookDecision(allowed=True)

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        _log(f"Malformed IPC response: {text}")
        return HookDecision(allowed=True)

    decision = data.get("decision", "allow")
    reason = data.get("reason")

    if decision == "deny":
        return HookDecision(allowed=False, reason=reason)

    # "allow" or anything else -> allow
    return HookDecision(allowed=True)


async def bash_security_hook(tool_name: str, tool_input: dict) -> HookDecision:
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
