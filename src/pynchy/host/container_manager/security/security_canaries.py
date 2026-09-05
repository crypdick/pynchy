"""Harmless operational self-checks for critical security control surfaces."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from pynchy.canary_contracts import CanaryExercise, CanaryRunContext, CanaryScenario
from pynchy.host.container_manager.api import (
    BuiltinGateway,
    CopContextAvailability,
    CopInspectionContext,
    CopVerdict,
    GatewayRedactionPosture,
    PackageCoordinate,
    PackageEcosystem,
    PackageIntent,
    PackageMetadataAssessment,
    PackageMetadataState,
    PackageSource,
    ReceiptVerification,
    RegistryMetadataError,
    assess_package_metadata,
    clear_approval_receipts,
    clear_package_metadata_cache,
    consume_approval_receipt,
    cop_requires_human,
    evaluate_package_coordinates,
    guarded_action_id,
    issue_approval_receipt,
    redaction_posture_for_gateway_mode,
)
from pynchy.host.container_manager.security.artifact_canaries import FileSecretTaintCanary


class SecurityCanaryError(RuntimeError):
    """A local security control did not exhibit its required behavior."""


@runtime_checkable
class _CanaryRegistration(Protocol):
    def __call__(self, scenario_id: str, scenario: CanaryScenario) -> None: ...


class _NoCleanupScenario:
    async def cleanup(
        self,
        _context: CanaryRunContext,
        _exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True)
class _DecisionArtifact:
    decision: str


class DeterministicHardBlockCanary(_NoCleanupScenario):
    """Prove a shell-evaluated package coordinate is denied deterministically."""

    async def exercise(self, _context: CanaryRunContext) -> CanaryExercise:
        coordinate = PackageCoordinate(
            PackageEcosystem.PYPI,
            None,
            None,
            PackageSource.SHELL,
            PackageIntent.DEPENDENCY,
            lock_pinned=False,
        )
        decision, rule_ids = await evaluate_package_coordinates((coordinate,))
        return CanaryExercise(
            artifact=_DecisionArtifact(decision["decision"]),
            evidence_refs=tuple(f"security:rule:{rule_id}:observed" for rule_id in rule_ids),
        )

    async def verify(
        self,
        _context: CanaryRunContext,
        exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        if not isinstance(exercise.artifact, _DecisionArtifact):
            raise SecurityCanaryError("Unexpected deterministic canary artifact")
        if exercise.artifact.decision != "deny":
            raise SecurityCanaryError("Deterministic rule did not deny the request")
        return ("security:deterministic:hard-block",)


@dataclass(frozen=True)
class _ApprovalArtifact:
    mutation: ReceiptVerification
    exact: ReceiptVerification
    replay: ReceiptVerification


class ApprovalMutationReplayCanary:
    """Prove payload mutation and receipt replay fail while exact use succeeds once."""

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        clear_approval_receipts()
        request = {"type": "register_group", "request_id": context.run_id, "name": "safe"}
        mutation_token = issue_approval_receipt(
            action_id=guarded_action_id(context.run_id),
            workspace="security-canary",
            operation="register_group",
            request_data=request,
        )
        mutated = {**request, "prompt": "changed", "_approval_receipt": str(mutation_token)}
        mutation = consume_approval_receipt(
            mutated,
            workspace="security-canary",
            operation="register_group",
        )
        exact_token = issue_approval_receipt(
            action_id=guarded_action_id(context.run_id),
            workspace="security-canary",
            operation="register_group",
            request_data=request,
        )
        exact = consume_approval_receipt(
            {**request, "_approval_receipt": str(exact_token)},
            workspace="security-canary",
            operation="register_group",
        )
        replay = consume_approval_receipt(
            {**request, "_approval_receipt": str(exact_token)},
            workspace="security-canary",
            operation="register_group",
        )
        return CanaryExercise(artifact=_ApprovalArtifact(mutation, exact, replay))

    async def verify(
        self,
        _context: CanaryRunContext,
        exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        expected = _ApprovalArtifact(
            ReceiptVerification.INVALID,
            ReceiptVerification.VALID,
            ReceiptVerification.INVALID,
        )
        if exercise.artifact != expected:
            raise SecurityCanaryError("Approval binding or single-use receipt contract failed")
        return (
            "security:approval:mutation:invalid",
            "security:approval:exact:valid",
            "security:approval:replay:invalid",
        )

    async def cleanup(
        self,
        _context: CanaryRunContext,
        _exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        clear_approval_receipts()
        return ()


class CopDegradedApprovalCanary(_NoCleanupScenario):
    """Prove Cop failure and bounded-context loss both require approval."""

    async def exercise(self, _context: CanaryRunContext) -> CanaryExercise:
        clear_context = CopInspectionContext(CopContextAvailability.AVAILABLE)
        missing_context = CopInspectionContext(CopContextAvailability.UNAVAILABLE)
        artifact = (
            cop_requires_human(CopVerdict(flagged=False, degraded=True), clear_context),
            cop_requires_human(CopVerdict(flagged=False), missing_context),
        )
        return CanaryExercise(artifact=artifact)

    async def verify(
        self,
        _context: CanaryRunContext,
        exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        if exercise.artifact != (True, True):
            raise SecurityCanaryError("Degraded Cop did not require human approval")
        return ("security:cop:degraded:approval",)


@dataclass(frozen=True)
class _GatewayArtifact:
    builtin: GatewayRedactionPosture  # noqa: V107
    litellm: GatewayRedactionPosture
    secret_removed: bool  # noqa: V107


class GatewayPostureCanary(_NoCleanupScenario):
    """Prove the owned and external gateway modes report distinct postures."""

    async def exercise(self, _context: CanaryRunContext) -> CanaryExercise:
        gateway = BuiltinGateway(port=0, host="127.0.0.1", container_host="localhost")
        secret = b"canary-secret-value-1234567890"
        body = b'{"messages":[{"role":"user","content":"api_key=' + secret + b'"}]}'
        redacted = gateway.prepare_upstream_body(body)
        artifact = _GatewayArtifact(
            builtin=redaction_posture_for_gateway_mode("builtin"),
            litellm=redaction_posture_for_gateway_mode("litellm"),
            secret_removed=secret not in redacted,
        )
        return CanaryExercise(artifact=artifact)

    async def verify(
        self,
        _context: CanaryRunContext,
        exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        expected = _GatewayArtifact(
            builtin=GatewayRedactionPosture.ENFORCED,
            litellm=GatewayRedactionPosture.NOT_ENFORCED,
            secret_removed=True,
        )
        if exercise.artifact != expected:
            raise SecurityCanaryError("Gateway redaction posture contract failed")
        return (
            "security:gateway:builtin:enforced",
            "security:gateway:litellm:not-enforced",
            "security:gateway:restoration:irreversible",
        )


@dataclass(frozen=True)
class _PackageMetadataArtifact:
    fresh: PackageMetadataState
    degraded: PackageMetadataState
    fresh_decision: str
    degraded_decision: str
    lock_decision: str


class PackageMetadataCanary:
    """Prove fresh and unavailable authoritative metadata stay distinct."""

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        clear_package_metadata_cache()
        now = datetime.now(UTC)
        coordinate = PackageCoordinate(
            PackageEcosystem.PYPI,
            f"pynchy-canary-{context.run_id}",
            "1.0.0",
            PackageSource.REGISTRY,
            PackageIntent.EXECUTABLE,
            lock_pinned=False,
        )

        async def fresh_fetcher(_coordinate: PackageCoordinate) -> datetime:
            await asyncio.sleep(0)
            return now - timedelta(days=1)

        async def degraded_fetcher(_coordinate: PackageCoordinate) -> datetime:
            await asyncio.sleep(0)
            raise RegistryMetadataError("synthetic bounded outage")

        fresh = await assess_package_metadata(coordinate, now=now, fetcher=fresh_fetcher)
        clear_package_metadata_cache()
        degraded = await assess_package_metadata(coordinate, now=now, fetcher=degraded_fetcher)

        async def fresh_assessor(_coordinate: PackageCoordinate) -> PackageMetadataAssessment:
            await asyncio.sleep(0)
            return fresh

        async def degraded_assessor(_coordinate: PackageCoordinate) -> PackageMetadataAssessment:
            await asyncio.sleep(0)
            return degraded

        fresh_decision, _fresh_rules = await evaluate_package_coordinates(
            (coordinate,),
            assessor=fresh_assessor,
        )
        degraded_decision, _degraded_rules = await evaluate_package_coordinates(
            (coordinate,),
            assessor=degraded_assessor,
        )
        lock_coordinate = PackageCoordinate(
            coordinate.ecosystem,
            coordinate.name,
            coordinate.version,
            coordinate.source,
            PackageIntent.RECONCILIATION,
            lock_pinned=True,
        )
        lock_decision, _lock_rules = await evaluate_package_coordinates(
            (lock_coordinate,),
            assessor=degraded_assessor,
        )
        return CanaryExercise(
            artifact=_PackageMetadataArtifact(
                fresh=fresh.state,
                degraded=degraded.state,
                fresh_decision=fresh_decision["decision"],
                degraded_decision=degraded_decision["decision"],
                lock_decision=lock_decision["decision"],
            ),
        )

    async def verify(
        self,
        _context: CanaryRunContext,
        exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        expected = _PackageMetadataArtifact(
            fresh=PackageMetadataState.FRESH,
            degraded=PackageMetadataState.DEGRADED,
            fresh_decision="needs_human",
            degraded_decision="needs_human",
            lock_decision="allow",
        )
        if exercise.artifact != expected:
            raise SecurityCanaryError("Package metadata age/degraded contract failed")
        return (
            "security:package:fresh:approval",
            "security:package:degraded:approval",
            "security:package:degraded-lock:allow-audited",
        )

    async def cleanup(
        self,
        _context: CanaryRunContext,
        _exercise: CanaryExercise,
    ) -> tuple[str, ...]:
        clear_package_metadata_cache()
        return ()


def register_security_canary_scenarios(register: _CanaryRegistration) -> None:
    """Register harmless local security assurance checks."""
    register("security.deterministic.hard-block", DeterministicHardBlockCanary())
    register("security.file-secret-taint", FileSecretTaintCanary())
    register("security.approval.mutation-replay", ApprovalMutationReplayCanary())
    register("security.cop.degraded-approval", CopDegradedApprovalCanary())
    register("security.gateway.posture", GatewayPostureCanary())
    register("security.package.metadata", PackageMetadataCanary())
