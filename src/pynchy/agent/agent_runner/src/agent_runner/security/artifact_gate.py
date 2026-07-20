"""Cross-tool deterministic security gate and workspace file-taint notifier."""

from __future__ import annotations

import json
import sys
from typing import Any

from agent_runner.hooks import HookDecision
from agent_runner.security.action_identity import current_guarded_action_id
from agent_runner.security.artifacts import deterministic_findings, normalize_tool_request


def _log(message: str) -> None:
    sys.stderr.write(f"[artifact-gate] {message}\n")
    sys.stderr.flush()


async def _notify_host(
    tool_name: str,
    *,
    rule_ids: tuple[str, ...],
    packages: tuple[dict[str, str | bool | None], ...],
) -> HookDecision:
    from agent_runner.agent_tools._ipc_request import (  # noqa: PLC0415, PLC2701, RUF100 - avoid loading IPC until a file-capable tool runs.
        ipc_service_request,
    )

    try:
        results = await ipc_service_request(
            "artifact_check",
            {
                "tool_name": tool_name,
                "rule_ids": list(rule_ids),
                "file_access": True,
                "packages": list(packages),
            },
            response_timeout_seconds=30,
            type_override="security:artifact_check",
            guarded_action_id=str(current_guarded_action_id()),
        )
    except Exception as exc:  # allow: exception-handling  # noqa: BLE001, RUF100
        reason = f"Host artifact notification failed closed: {type(exc).__name__}"
        _log(reason)
        return HookDecision(allowed=False, reason=reason)
    if not results:
        reason = "Host returned an empty artifact decision; failing closed"
        _log(reason)
        return HookDecision(allowed=False, reason=reason)
    text = results[0].text
    if text.startswith("Error:"):
        reason = f"Host artifact notification unavailable; failing closed: {text}"
        _log(reason)
        return HookDecision(allowed=False, reason=reason)
    try:
        response = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        reason = "Host returned a malformed artifact decision; failing closed"
        _log(reason)
        return HookDecision(allowed=False, reason=reason)
    if response.get("decision") == "deny":
        return HookDecision(allowed=False, reason=str(response.get("reason") or "Host policy"))
    return HookDecision(allowed=True)


async def artifact_security_hook(tool_name: str, tool_input: dict[str, Any]) -> HookDecision:
    """Enforce deterministic rules and notify sticky host taint before execution."""
    request = normalize_tool_request(tool_name, tool_input)
    findings = deterministic_findings(request)
    host_reviewed = frozenset({"CRED001", "PKG001", "PKG004"})
    blocking = tuple(finding for finding in findings if finding.rule_id not in host_reviewed)
    if blocking:
        reason = "; ".join(f"{finding.rule_id}: {finding.reason}" for finding in blocking)
        _log(f"Denied {tool_name} by deterministic rules: {reason}")
        return HookDecision(allowed=False, reason=reason)

    if not request.accesses_files:
        return HookDecision(allowed=True)

    return await _notify_host(
        tool_name,
        rule_ids=tuple(f.rule_id for f in findings),
        packages=tuple(package.to_wire() for package in request.packages),
    )
