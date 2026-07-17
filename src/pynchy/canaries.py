"""Execute declared external-service canaries and render their evidence report."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import (
    Mapping,  # noqa: TC003, RUF100 - beartype resolves canary runner annotations at runtime.
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from pynchy.actions import ACTION_SPECS
from pynchy.host.git_ops.utils import get_head_sha
from pynchy.logger import logger
from pynchy.state import (
    get_latest_canary_runs,
    get_recent_canary_runs,
    get_unresolved_canary_regressions,
    record_canary_run,
)
from pynchy.types import CanaryOutcome, CanaryRun

_CLEANUP_ATTEMPTS = 3
_SCENARIO_EXECUTORS: dict[str, CanaryScenario] = {}


@dataclass(frozen=True)
class CanaryRunContext:
    """Context supplied to a scenario's exercise, verifier, and cleanup steps."""

    run_id: str
    scenario_id: str
    target_profile: str
    scheduler_deps: object | None


@dataclass(frozen=True)
class CanaryExercise:
    """Opaque exercise artifact and safe evidence references for a scenario."""

    artifact: object
    evidence_refs: tuple[str, ...] = ()


@runtime_checkable
class CanaryScenario(Protocol):
    """A real-service scenario with independent verification and cleanup."""

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise:
        """Perform the scenario action against the dedicated test target."""

    async def verify(self, context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        """Verify remote state without relying on the agent's completion text."""

    async def cleanup(self, context: CanaryRunContext, exercise: CanaryExercise) -> tuple[str, ...]:
        """Remove the scenario artifact; implementations must be idempotent."""


class CanarySkippedError(RuntimeError):
    """Signal that a configured scenario cannot run at this time without failing it."""


def declared_canary_actions() -> dict[str, tuple[str, ...]]:
    """Map each declared scenario to the semantic actions it proves."""
    result: dict[str, list[str]] = {}
    for spec in ACTION_SPECS:
        if spec.canary_scenario is not None:
            result.setdefault(spec.canary_scenario, []).append(str(spec.id))
    return {scenario_id: tuple(action_ids) for scenario_id, action_ids in sorted(result.items())}


def register_canary_scenario(scenario_id: str, scenario: CanaryScenario) -> None:
    """Register an executable scenario implementation from Pynchy or a plugin."""
    if scenario_id not in declared_canary_actions():
        raise ValueError(f"Canary scenario is not declared by an action: {scenario_id}")
    if scenario_id in _SCENARIO_EXECUTORS:
        raise ValueError(f"Canary scenario already registered: {scenario_id}")
    _SCENARIO_EXECUTORS[scenario_id] = scenario


def registered_canary_scenarios() -> dict[str, CanaryScenario]:
    """Return a copy of the currently executable scenario registry."""
    return dict(_SCENARIO_EXECUTORS)


@dataclass(frozen=True)
class _CanaryRunRequest:
    scenario_id: str
    action_ids: tuple[str, ...]
    target_profile: str
    scheduler_deps: object | None
    scenario: CanaryScenario | None
    code_revision: str
    config_revision: str


async def run_declared_canaries(
    *,
    target_profile: str,
    scheduler_deps: object | None = None,
    executors: Mapping[str, CanaryScenario] | None = None,
    code_revision: str | None = None,
    config_revision: str | None = None,
) -> list[CanaryRun]:
    """Run every declared scenario and persist its independently classified result."""
    scenario_actions = declared_canary_actions()
    active_executors = _SCENARIO_EXECUTORS if executors is None else executors
    revisions = (
        code_revision or _code_revision(),
        config_revision or _config_revision(),
    )
    results: list[CanaryRun] = []
    for scenario_id, action_ids in scenario_actions.items():
        result = await _run_one_canary(
            _CanaryRunRequest(
                scenario_id=scenario_id,
                action_ids=action_ids,
                target_profile=target_profile,
                scheduler_deps=scheduler_deps,
                scenario=active_executors.get(scenario_id),
                code_revision=revisions[0],
                config_revision=revisions[1],
            )
        )
        results.append(await record_canary_run(result))
    return results


async def get_canary_report(*, history_limit: int = 50) -> dict[str, object]:
    """Return declared coverage, recent history, and unresolved regressions."""
    latest, recent_runs, unresolved = await asyncio.gather(
        get_latest_canary_runs(),
        get_recent_canary_runs(limit=history_limit),
        get_unresolved_canary_regressions(),
    )
    latest_by_scenario: dict[str, list[dict[str, object]]] = {}
    for run in latest:
        latest_by_scenario.setdefault(run.scenario_id, []).append(_canary_run_dict(run))
    declared = declared_canary_actions()
    return {
        "summary": {
            "declared_scenarios": len(declared),
            "established_targets": sum(run.outcome is CanaryOutcome.PASSED for run in latest),
            "not_established_targets": sum(
                run.outcome is CanaryOutcome.NOT_ESTABLISHED for run in latest
            ),
            "unresolved_regressions": len(unresolved),
        },
        "scenarios": [
            {
                "id": scenario_id,
                "action_ids": action_ids,
                "latest_runs": latest_by_scenario.get(scenario_id, []),
            }
            for scenario_id, action_ids in declared.items()
        ],
        "unresolved_regressions": [_canary_run_dict(run) for run in unresolved],
        "recent_runs": [_canary_run_dict(run) for run in recent_runs],
    }


def canary_run_to_dict(run: CanaryRun) -> dict[str, object]:
    """Serialize persisted canary evidence without exposing raw exceptions."""
    return _canary_run_dict(run)


async def _run_one_canary(request: _CanaryRunRequest) -> CanaryRun:
    started_at = datetime.now(UTC)
    context = CanaryRunContext(
        run_id=str(uuid4()),
        scenario_id=request.scenario_id,
        target_profile=request.target_profile,
        scheduler_deps=request.scheduler_deps,
    )
    outcome = CanaryOutcome.NOT_ESTABLISHED
    error_class: str | None = None
    evidence_refs: tuple[str, ...] = ()
    exercise: CanaryExercise | None = None
    if request.scenario is not None:
        outcome, error_class, evidence_refs, exercise = await _exercise_and_verify(
            request.scenario, context
        )
        if exercise is not None:
            cleanup_outcome, cleanup_error, cleanup_refs = await _cleanup_scenario(
                request.scenario, context, exercise
            )
            evidence_refs = _deduplicated_refs((*evidence_refs, *cleanup_refs))
            if cleanup_outcome is not None:
                outcome = cleanup_outcome
                error_class = cleanup_error
    return CanaryRun(
        run_id=context.run_id,
        scenario_id=request.scenario_id,
        action_ids=request.action_ids,
        target_profile=request.target_profile,
        code_revision=request.code_revision,
        config_revision=request.config_revision,
        started_at=started_at.isoformat(),
        completed_at=datetime.now(UTC).isoformat(),
        outcome=outcome,
        error_class=error_class,
        evidence_refs=evidence_refs,
    )


async def _exercise_and_verify(
    scenario: CanaryScenario, context: CanaryRunContext
) -> tuple[CanaryOutcome, str | None, tuple[str, ...], CanaryExercise | None]:
    exercise: CanaryExercise | None = None
    try:
        exercise = await scenario.exercise(context)
    except CanarySkippedError:
        return CanaryOutcome.SKIPPED, None, (), None
    except Exception as exc:  # noqa: BLE001, RUF100 - external canary failure must be redacted and persisted.
        error_class = type(exc).__name__
        logger.warning(
            "Canary exercise failed",
            scenario_id=context.scenario_id,
            error_class=error_class,
        )
        return CanaryOutcome.FAILED, error_class, (), None
    try:
        verifier_refs = await scenario.verify(context, exercise)
    except CanarySkippedError:
        return CanaryOutcome.SKIPPED, None, exercise.evidence_refs, exercise
    except Exception as exc:  # noqa: BLE001, RUF100 - verifier failure still requires artifact cleanup.
        error_class = type(exc).__name__
        logger.warning(
            "Canary verification failed",
            scenario_id=context.scenario_id,
            error_class=error_class,
        )
        return CanaryOutcome.FAILED, error_class, exercise.evidence_refs, exercise
    return (
        CanaryOutcome.PASSED,
        None,
        _deduplicated_refs((*exercise.evidence_refs, *verifier_refs)),
        exercise,
    )


async def _cleanup_scenario(
    scenario: CanaryScenario, context: CanaryRunContext, exercise: CanaryExercise
) -> tuple[CanaryOutcome | None, str | None, tuple[str, ...]]:
    error_class = "UnknownCleanupFailure"
    for _attempt in range(_CLEANUP_ATTEMPTS):
        try:
            return None, None, _deduplicated_refs(await scenario.cleanup(context, exercise))
        except Exception as exc:  # noqa: BLE001, RUF100 - cleanup gets independent retries before recording failure.
            error_class = type(exc).__name__
            logger.warning(
                "Canary cleanup failed",
                scenario_id=context.scenario_id,
                error_class=error_class,
            )
    return CanaryOutcome.CLEANUP_FAILED, error_class, ()


def _deduplicated_refs(refs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def _code_revision() -> str:
    return get_head_sha() or "unknown"


def _config_revision() -> str:
    config_path = Path.cwd() / "config.toml"
    try:
        return hashlib.sha256(config_path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unknown"


def _canary_run_dict(run: CanaryRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "scenario_id": run.scenario_id,
        "action_ids": run.action_ids,
        "target_profile": run.target_profile,
        "code_revision": run.code_revision,
        "config_revision": run.config_revision,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "outcome": run.outcome.value,
        "error_class": run.error_class,
        "evidence_refs": run.evidence_refs,
        "is_regression": run.is_regression,
        "starts_regression": run.starts_regression,
        "is_recovery": run.is_recovery,
    }
