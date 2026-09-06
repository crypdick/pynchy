"""The Cop: LLM-based security inspector for host-mutating operations.

Inspects at two points in the pipeline:
- Inbound: content from public_source services, scanned for injection patterns
- Outbound: host-mutating payloads (diffs, configs, prompts), scanned for manipulation

Calls are routed through the LLM gateway (LiteLLM or builtin proxy) so the
cop doesn't need its own API credentials and all usage shows up in the
gateway's spend tracking.

See docs/plans/2026-02-24-host-mutating-cop-design.md
"""

from __future__ import annotations

import json as _json
from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from enum import StrEnum

from pynchy.host.container_manager.security.cop_client import (
    CopGatewayUnavailableError,
    request_inspection,
)
from pynchy.logger import logger
from pynchy.redaction import RedactionSession
from pynchy.security_context import (
    RecentSecurityContext,
    SecurityExecutionAuthority,
)

CopPromptProvider = Callable[[str], str]


def _unconfigured_prompt_provider(_field: str) -> str:
    raise RuntimeError("Cop prompts have not been composed")


_prompt_provider: CopPromptProvider = _unconfigured_prompt_provider


def configure_cop_prompt_provider(provider: CopPromptProvider) -> None:
    """Bind live prompt resolution at host composition."""
    global _prompt_provider  # noqa: PLW0603 - one host process owns Cop configuration.
    _prompt_provider = provider


class CopContextAvailability(StrEnum):
    """Whether bounded session context could be loaded safely."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CopInspectionContext:
    """Bounded intent and action-chain context for one proposed action."""

    availability: CopContextAvailability
    current_user_intent: str | None = None
    recent_messages: tuple[tuple[str, str], ...] = ()
    recent_agent_updates: tuple[str, ...] = ()
    completed_tool_actions: tuple[str, ...] = ()
    execution_authority: SecurityExecutionAuthority | None = None
    unavailable_reason: str | None = None


async def load_cop_inspection_context(
    chat_jid: str,
    load_recent_security_context: Callable[..., Awaitable[RecentSecurityContext]],
) -> CopInspectionContext:
    """Load bounded SQLite context or return an explicit degraded value."""
    try:
        context = await load_recent_security_context(chat_jid)
    except Exception as exc:  # noqa: BLE001 - context loss becomes a typed degraded policy input.
        logger.warning(
            "Cop context unavailable",
            chat_jid=chat_jid,
            error_type=type(exc).__name__,
        )
        return CopInspectionContext(
            availability=CopContextAvailability.UNAVAILABLE,
            unavailable_reason=type(exc).__name__,
        )
    return _inspection_context_from_recent(context)


def _inspection_context_from_recent(context: RecentSecurityContext) -> CopInspectionContext:
    return CopInspectionContext(
        availability=CopContextAvailability.AVAILABLE,
        current_user_intent=context.current_user_intent,
        recent_messages=tuple(
            (str(message.role), message.content) for message in context.recent_messages
        ),
        recent_agent_updates=context.recent_agent_updates,
        completed_tool_actions=context.completed_tool_actions,
        execution_authority=context.execution_authority,
    )


@dataclass(frozen=True)
class CopVerdict:
    """Result of a Cop inspection."""

    flagged: bool
    reason: str | None = None
    degraded: bool = False


class CopCommandDecision(StrEnum):
    """High-confidence command decisions available to the Cop."""

    APPROVE = "approve"
    DENY = "deny"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class CopCommandVerdict:
    """The Cop's decision for one exact shell command."""

    decision: CopCommandDecision
    reason: str
    degraded: bool = False


class CopTaintDecision(StrEnum):
    """Semantic decisions for one heuristic secret-taint candidate."""

    CONFIRM = "confirm"
    REJECT = "reject"  # noqa: V107


@dataclass(frozen=True)
class CopTaintCandidate:
    """Bounded rule evidence for a possible secret exposure."""

    rule_id: str
    artifact_kind: str
    artifact_value: str


@dataclass(frozen=True)
class CopTaintVerdict:
    """The Cop's decision about whether candidate evidence establishes taint."""

    decision: CopTaintDecision
    reason: str
    degraded: bool = False


@dataclass(frozen=True)
class CopCommandRisk:
    """Host-owned security facts for one command review."""

    network_capable: bool
    corruption_tainted: bool
    secret_tainted: bool


def _configured_prompt(field: str) -> str:
    return _prompt_provider(field)


async def inspect_outbound(
    operation: str,
    payload_summary: str,
    inspection_context: CopInspectionContext | None = None,
) -> CopVerdict:
    """Inspect a host-mutating operation payload for manipulation.

    Args:
        operation: The IPC operation name (e.g., "sync_worktree_to_main")
        payload_summary: Human-readable summary of what the operation will do
            (e.g., the git diff, the task prompt, the group config)
    """
    return await _inspect(
        system_prompt=_configured_prompt("cop_outbound"),
        user_content=_action_review_content(
            operation,
            payload_summary,
            inspection_context,
        ),
        context=f"outbound:{operation}",
    )


async def inspect_inbound(
    source: str,
    content: str,
) -> CopVerdict:
    """Inspect inbound content from an untrusted source for injection.

    Args:
        source: Description of the source (e.g., "email from stranger@evil.com")
        content: The untrusted content to inspect
    """
    return await _inspect(
        system_prompt=_configured_prompt("cop_inbound"),
        user_content=f"Source: {source}\n\nContent:\n{content[:5000]}",
        context=f"inbound:{source}",
    )


async def inspect_bash(
    command: str,
    inspection_context: CopInspectionContext | None = None,
    risk: CopCommandRisk | None = None,
) -> CopCommandVerdict:
    """Approve, deny, or escalate one exact Bash command.

    Args:
        command: The full bash command string the agent wants to execute.
    """
    context = f"bash:{command[:100]}"
    try:
        result = await request_inspection(
            system_prompt=_configured_prompt("cop_bash"),
            user_content=_command_review_content(command, inspection_context, risk),
        )
        verdict = _parse_command_verdict(result)
    except Exception as exc:  # noqa: BLE001 - failures become typed degraded policy input.
        logger.error(
            "Cop command inspection failed; escalating",
            context=context,
            error_type=type(exc).__name__,
        )
        return CopCommandVerdict(
            decision=CopCommandDecision.ESCALATE,
            reason=_cop_failure_reason(exc),
            degraded=True,
        )

    logger.info(
        "Cop command inspection complete",
        context=context,
        decision=verdict.decision.value,
        reason=verdict.reason,
    )
    return verdict


async def inspect_secret_taint(
    tool_name: str,
    candidates: tuple[CopTaintCandidate, ...],
) -> CopTaintVerdict:
    """Confirm or reject heuristic credential-access evidence.

    Invalid output and transport failures conservatively confirm taint. The
    operation itself is not denied: confirmed taint only affects later policy.
    """
    context = f"secret-taint:{tool_name}"
    try:
        result = await request_inspection(
            system_prompt=_configured_prompt("cop_taint"),
            user_content=_taint_review_content(tool_name, candidates),
        )
        verdict = _parse_taint_verdict(result)
    except Exception as exc:  # noqa: BLE001 - degraded review confirms taint.
        logger.error(
            "Cop taint inspection failed; confirming conservatively",
            context=context,
            error_type=type(exc).__name__,
        )
        return CopTaintVerdict(
            decision=CopTaintDecision.CONFIRM,
            reason=_cop_failure_reason(exc),
            degraded=True,
        )

    logger.info(
        "Cop taint inspection complete",
        context=context,
        decision=verdict.decision.value,
        reason=verdict.reason,
    )
    return verdict


def _parse_command_verdict(result: dict[str, object]) -> CopCommandVerdict:
    raw_decision = result.get("decision")
    if not isinstance(raw_decision, str):
        raise TypeError("Cop command verdict omitted its decision")
    decision = CopCommandDecision(raw_decision)
    reason = result.get("reason")
    if not isinstance(reason, str):
        raise TypeError("Cop command verdict omitted its reason")
    if not reason.strip():
        raise ValueError("Cop command verdict reason cannot be empty")
    return CopCommandVerdict(decision=decision, reason=reason.strip())


def _parse_taint_verdict(result: dict[str, object]) -> CopTaintVerdict:
    raw_decision = result.get("decision")
    if not isinstance(raw_decision, str):
        raise TypeError("Cop taint verdict omitted its decision")
    decision = CopTaintDecision(raw_decision)
    reason = result.get("reason")
    if not isinstance(reason, str):
        raise TypeError("Cop taint verdict omitted its reason")
    if not reason.strip():
        raise ValueError("Cop taint verdict reason cannot be empty")
    return CopTaintVerdict(decision=decision, reason=reason.strip())


def _taint_review_content(
    tool_name: str,
    candidates: tuple[CopTaintCandidate, ...],
) -> str:
    redaction = RedactionSession()
    payload = {
        "tool_name": tool_name,
        "candidates": [
            {
                "rule_id": candidate.rule_id,
                "artifact_kind": candidate.artifact_kind,
                "artifact_value": redaction.redact_text(candidate.artifact_value).value,
            }
            for candidate in candidates
        ],
    }
    return f"Heuristic taint candidates:\n{_json.dumps(payload, ensure_ascii=False)}"


def _command_review_content(
    command: str,
    inspection_context: CopInspectionContext | None,
    risk: CopCommandRisk | None,
) -> str:
    risk_payload = (
        {
            "network_capable": risk.network_capable,
            "corruption_tainted": risk.corruption_tainted,
            "secret_tainted": risk.secret_tainted,
        }
        if risk is not None
        else {"availability": "not supplied"}
    )
    return (
        f"{_action_review_content('Bash', command, inspection_context)}\n\n"
        f"Host security facts:\n{_json.dumps(risk_payload)}"
    )


def _action_review_content(
    operation: str,
    proposed_action: str,
    inspection_context: CopInspectionContext | None,
) -> str:
    context = inspection_context or CopInspectionContext(
        availability=CopContextAvailability.UNAVAILABLE,
        unavailable_reason="context not supplied",
    )
    context_payload = {
        "availability": context.availability.value,
        "current_user_intent": context.current_user_intent,
        "recent_messages": [
            {"role": role, "content": content} for role, content in context.recent_messages
        ],
        "recent_agent_updates": list(context.recent_agent_updates),
        "completed_tool_actions": list(context.completed_tool_actions),
        "durable_execution_authority": (
            {
                "kind": context.execution_authority.kind.value,
                "work_item_identifier": context.execution_authority.work_item_identifier,
                "authorized_scope": [
                    "implement and validate the leased work item",
                    "publish its isolated worktree branch as a pull request for review",
                    "attach the pull request and update the approved work-item lifecycle",
                ],
                "excluded_scope": [
                    "merge a pull request",
                    "deploy to production",
                    "perform unrelated external writes",
                ],
            }
            if context.execution_authority is not None
            else None
        ),
        "unavailable_reason": context.unavailable_reason,
    }
    return (
        f"Operation: {operation}\n\n"
        f"Bounded context:\n{_json.dumps(context_payload, ensure_ascii=False)}\n\n"
        f"Proposed action:\n{proposed_action}"
    )


async def _inspect(
    system_prompt: str,
    user_content: str,
    context: str,
) -> CopVerdict:
    """Run an LLM inspection and return a CopVerdict."""
    try:
        verdict = await _run_inspection(system_prompt, user_content, context)
    except Exception as exc:  # noqa: BLE001 - failures become typed degraded policy input.
        logger.error(
            "Cop inspection failed; returning degraded verdict",
            context=context,
            error_type=type(exc).__name__,
        )
        return CopVerdict(
            flagged=False,
            reason=_cop_failure_reason(exc),
            degraded=True,
        )
    else:
        return verdict


async def _run_inspection(
    system_prompt: str,
    user_content: str,
    context: str,
) -> CopVerdict:
    result = await request_inspection(system_prompt=system_prompt, user_content=user_content)
    flagged = result.get("flagged")
    if not isinstance(flagged, bool):
        raise TypeError("Cop verdict omitted its flagged decision")
    raw_reason = result.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else None
    verdict = CopVerdict(
        flagged=flagged,
        reason=reason,
    )

    logger.info(
        "Cop inspection complete",
        context=context,
        flagged=verdict.flagged,
        reason=verdict.reason,
    )
    return verdict


def _cop_failure_reason(exc: Exception) -> str:
    if isinstance(exc, CopGatewayUnavailableError):
        return str(exc)
    return f"Cop unavailable: {type(exc).__name__}"
