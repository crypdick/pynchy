"""IPC enforcement for file taint and typed package artifacts."""

from __future__ import annotations

from collections.abc import (
    Awaitable,
    Callable,
)
from dataclasses import dataclass
from pathlib import (
    Path,
)
from typing import Any, Protocol, runtime_checkable

from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.container_manager.security.audit import record_security_event
from pynchy.host.container_manager.security.cop import (
    CopTaintCandidate,
    CopTaintDecision,
    inspect_secret_taint,
)
from pynchy.host.container_manager.security.gate import SecurityGate, get_gate_for_group
from pynchy.host.container_manager.security.package_metadata import (
    PackageCoordinate,
    PackageIntent,
    PackageMetadataAssessment,
    PackageMetadataState,
    PackageSource,
    assess_package_metadata,
)
from pynchy.logger import logger
from pynchy.plugins.api import (
    OutboundEvent,
)
from pynchy.workspace.api import (
    WorkspaceProfile,
)

_MAX_TAINT_CANDIDATES = 8
_MAX_TAINT_ARTIFACT_CHARS = 4_000


@runtime_checkable
class _ArtifactSecurityDeps(Protocol):
    async def broadcast_to_channels(self, jid: str, event: OutboundEvent) -> None: ...

    def workspaces(self) -> dict[str, WorkspaceProfile]: ...


def _resolve_chat_jid(source_group: str, deps: _ArtifactSecurityDeps) -> str | None:
    return next(
        (jid for jid, workspace in deps.workspaces().items() if workspace.folder == source_group),
        None,
    )


@dataclass(frozen=True)
class _TaintAdjudication:
    audit_decision: str
    reason: str


def _allow() -> dict[str, str]:
    return {"decision": "allow"}


def _deny(reason: str) -> dict[str, str]:
    return {"decision": "deny", "reason": reason}


def _needs_human(reason: str) -> dict[str, str]:
    return {"decision": "needs_human", "reason": reason}


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _package_coordinates(value: object) -> tuple[PackageCoordinate, ...] | None:
    if not isinstance(value, list):
        return () if value is None else None
    coordinates = tuple(PackageCoordinate.from_wire(item) for item in value)
    if any(coordinate is None for coordinate in coordinates):
        return None
    return tuple(coordinate for coordinate in coordinates if coordinate is not None)


def _taint_evidence(value: object) -> tuple[CopTaintCandidate, ...] | None:
    if not isinstance(value, list) or not 0 < len(value) <= _MAX_TAINT_CANDIDATES:
        return None
    candidates: list[CopTaintCandidate] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        rule_id = item.get("rule_id")
        artifact_kind = item.get("artifact_kind")
        artifact_value = item.get("artifact_value")
        if (
            rule_id != "CRED001"
            or artifact_kind not in {"command", "path_read"}
            or not isinstance(artifact_value, str)
            or not artifact_value.strip()
            or len(artifact_value) > _MAX_TAINT_ARTIFACT_CHARS
        ):
            return None
        candidates.append(
            CopTaintCandidate(
                rule_id=rule_id,
                artifact_kind=artifact_kind,
                artifact_value=artifact_value,
            )
        )
    return tuple(candidates)


async def _adjudicate_credential_taint(
    *,
    data: dict[str, Any],
    tool_name: str,
    rule_ids: tuple[str, ...],
    gate: SecurityGate,
) -> _TaintAdjudication | None:
    if "CRED001" not in rule_ids or gate.secret_tainted:
        return None
    evidence = _taint_evidence(data.get("taint_evidence"))
    if evidence is not None and any(item.artifact_kind == "path_read" for item in evidence):
        gate.confirm_credential_access()
        return _TaintAdjudication(
            audit_decision="credential_taint_confirmed_by_rule",
            reason="A structured read targets a recognized credential path",
        )
    if not gate.cop_active:
        gate.confirm_credential_access()
        return _TaintAdjudication(
            audit_decision="credential_taint_confirmed_by_policy",
            reason="Cop is disabled; heuristic credential access confirmed conservatively",
        )
    if evidence is None:
        gate.confirm_credential_access()
        return _TaintAdjudication(
            audit_decision="credential_taint_confirmed_degraded",
            reason="Heuristic credential evidence missing or malformed",
        )

    verdict = await inspect_secret_taint(tool_name, evidence)
    if verdict.decision is CopTaintDecision.CONFIRM:
        gate.confirm_credential_access()
        return _TaintAdjudication(
            audit_decision=(
                "credential_taint_confirmed_degraded"
                if verdict.degraded
                else "credential_taint_confirmed"
            ),
            reason=verdict.reason,
        )
    return _TaintAdjudication(
        audit_decision="credential_taint_rejected",
        reason=verdict.reason,
    )


async def evaluate_package_coordinates(
    coordinates: tuple[PackageCoordinate, ...],
    *,
    assessor: Callable[[PackageCoordinate], Awaitable[PackageMetadataAssessment]] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Apply source, pinning, authoritative-age, and degraded-mode policy."""
    degraded_lock_reconciliation = False
    metadata_assessor = assessor or assess_package_metadata
    for coordinate in coordinates:
        static_decision = _static_package_decision(coordinate)
        if static_decision is not None:
            return static_decision
        assessment = await metadata_assessor(coordinate)
        if assessment.state is PackageMetadataState.FRESH:
            return _needs_human(assessment.reason), ("PKG006",)
        if assessment.state is PackageMetadataState.DEGRADED:
            if coordinate.intent is PackageIntent.RECONCILIATION and coordinate.lock_pinned:
                degraded_lock_reconciliation = True
                continue
            return _needs_human(assessment.reason), ("PKG005",)
    return _allow(), (("PKG005",) if degraded_lock_reconciliation else ())


def _static_package_decision(
    coordinate: PackageCoordinate,
) -> tuple[dict[str, str], tuple[str, ...]] | None:
    if coordinate.source in {PackageSource.SHELL, PackageSource.AMBIGUOUS}:
        return _deny("Package coordinate is ambiguous or shell-evaluated"), ("PKG002",)
    if coordinate.source in {
        PackageSource.DIRECT_URL,
        PackageSource.VCS,
        PackageSource.LOCAL,
        PackageSource.CUSTOM_REGISTRY,
    }:
        return _needs_human("Direct package sources require approval"), ("PKG001",)
    if coordinate.name is None:
        return _deny("Package name is missing"), ("PKG003",)
    if coordinate.intent is PackageIntent.EXECUTABLE and coordinate.version is None:
        return _needs_human("Unpinned executable package install requires approval"), ("PKG004",)
    return None


async def handle_artifact_security_check(
    data: dict[str, Any],
    source_group: str,
    is_admin: bool,  # noqa: FBT001 - registered prefix handler keeps the IPC dispatch contract.
    deps: _ArtifactSecurityDeps,
    *,
    response_path_override: Path | None = None,
) -> None:
    """Establish workspace file taint before a file-capable tool executes."""
    del is_admin
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        logger.warning("artifact_check missing request_id", source_group=source_group)
        return

    tool_name = data.get("tool_name")
    safe_tool_name = tool_name if isinstance(tool_name, str) and tool_name else "unknown"
    rule_ids = _string_tuple(data.get("rule_ids"))
    chat_jid = _resolve_chat_jid(source_group, deps) or "unknown"
    gate = get_gate_for_group(source_group)
    if gate is None:
        await _reject_missing_gate(
            source_group,
            request_id,
            safe_tool_name,
            chat_jid,
            rule_ids,
        )
        return

    coordinates = _package_coordinates(data.get("packages"))
    package_rule_ids: tuple[str, ...]
    if coordinates is None:
        decision = _deny("Malformed package coordinate payload")
        package_rule_ids = ("PKG003",)
    else:
        decision, package_rule_ids = await evaluate_package_coordinates(coordinates)
    audited_rule_ids = tuple(dict.fromkeys((*rule_ids, *package_rule_ids)))
    if data.get("file_access") is True:
        # Workspace declarations and service reads are host-owned facts. CRED001
        # is heuristic evidence, so the Cop adjudicates it before sticky taint.
        gate.notify_file_access()
        taint_adjudication = await _adjudicate_credential_taint(
            data=data,
            tool_name=safe_tool_name,
            rule_ids=rule_ids,
            gate=gate,
        )
        if taint_adjudication is not None:
            await record_security_event(
                chat_jid=chat_jid,
                workspace=source_group,
                tool_name=safe_tool_name,
                decision=taint_adjudication.audit_decision,
                corruption_tainted=gate.corruption_tainted,
                secret_tainted=gate.secret_tainted,
                reason=taint_adjudication.reason,
                request_id=request_id,
                rule_ids=("CRED001",),
            )

    if decision["decision"] == "needs_human":
        await _request_package_approval(
            source_group=source_group,
            request_id=request_id,
            tool_name=safe_tool_name,
            chat_jid=chat_jid,
            coordinates=coordinates or (),
            reason=decision.get("reason"),
            rule_ids=audited_rule_ids,
            gate=gate,
            deps=deps,
        )
        return

    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=safe_tool_name,
        decision=_artifact_audit_decision(decision, package_rule_ids),
        corruption_tainted=gate.corruption_tainted,
        secret_tainted=gate.secret_tainted,
        request_id=request_id,
        reason=decision.get("reason"),
        rule_ids=audited_rule_ids,
    )
    write_ipc_response(
        response_path_override or ipc_response_path(source_group, request_id),
        {"result": {**decision, "guarded_action_id": request_id}},
    )


async def _reject_missing_gate(
    source_group: str,
    request_id: str,
    tool_name: str,
    chat_jid: str,
    rule_ids: tuple[str, ...],
) -> None:
    reason = "No active security gate; artifact taint cannot be retained"
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=tool_name,
        decision="artifact_gate_unavailable",
        reason=reason,
        request_id=request_id,
        rule_ids=rule_ids,
    )
    write_ipc_response(
        ipc_response_path(source_group, request_id),
        {"result": {**_deny(reason), "guarded_action_id": request_id}},
    )


async def _request_package_approval(  # noqa: PLR0913 - approval boundary fields stay explicit.
    *,
    source_group: str,
    request_id: str,
    tool_name: str,
    chat_jid: str,
    coordinates: tuple[PackageCoordinate, ...],
    reason: str | None,
    rule_ids: tuple[str, ...],
    gate: SecurityGate,
    deps: _ArtifactSecurityDeps,
) -> None:
    from pynchy.host.container_manager.security.approval import (  # noqa: PLC0415 - avoid approval/import cycle.
        approval_event,
        create_pending_approval,
    )

    request_data: dict[str, Any] = {
        "packages": [
            {
                "ecosystem": coordinate.ecosystem.value,
                "name": coordinate.name,
                "version": coordinate.version,
                "source": coordinate.source.value,
                "intent": coordinate.intent.value,
                "lock_pinned": coordinate.lock_pinned,
            }
            for coordinate in coordinates
        ]
    }
    short_id = create_pending_approval(
        request_id=request_id,
        tool_name=tool_name,
        source_group=source_group,
        approval_chat_jid=chat_jid,
        request_data=request_data,
        handler_type="security_artifact",
    )
    await deps.broadcast_to_channels(
        chat_jid,
        approval_event(tool_name, request_data, short_id, preface=reason),
    )
    await record_security_event(
        chat_jid=chat_jid,
        workspace=source_group,
        tool_name=tool_name,
        decision="approval_requested",
        corruption_tainted=gate.corruption_tainted,
        secret_tainted=gate.secret_tainted,
        reason=reason,
        request_id=request_id,
        rule_ids=rule_ids,
    )


def _artifact_audit_decision(decision: dict[str, str], package_rule_ids: tuple[str, ...]) -> str:
    if "PKG005" in package_rule_ids and decision["decision"] == "allow":
        return "package_metadata_degraded"
    return decision["decision"] if decision["decision"] == "deny" else "file_access_noted"
