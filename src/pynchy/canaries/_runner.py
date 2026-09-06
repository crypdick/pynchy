"""Execute declared external-service canaries and render their evidence report."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Mapping,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pynchy.actions.api import ACTION_SPECS
from pynchy.canary_contracts import (
    CanaryExercise,
    CanaryOutcome,
    CanaryRun,
    CanaryRunContext,
    CanaryScenario,
    CanarySkippedError,
)
from pynchy.logger import logger
from pynchy.security_canary_ids import SECURITY_CANARY_IDS

_CLEANUP_ATTEMPTS = 3
_SCENARIO_EXECUTORS: dict[str, CanaryScenario] = {}


@dataclass(frozen=True)
class CanaryRuntime:
    """Durable evidence and revision capabilities selected at host composition."""

    record_run: Callable[[CanaryRun], Awaitable[CanaryRun]]
    latest_runs: Callable[[], Awaitable[list[CanaryRun]]]
    recent_runs: Callable[[int], Awaitable[list[CanaryRun]]]
    unresolved_regressions: Callable[[], Awaitable[list[CanaryRun]]]
    code_revision: Callable[[], str]


_runtime: CanaryRuntime | None = None


def configure_canary_runtime(runtime: CanaryRuntime) -> None:
    """Inject canary persistence and source revision capabilities."""
    global _runtime  # noqa: PLW0603 - one host process owns one canary runtime.
    _runtime = runtime


def _configured_runtime() -> CanaryRuntime:
    if _runtime is None:
        raise RuntimeError("canary runtime has not been configured")
    return _runtime


def declared_canary_actions() -> dict[str, tuple[str, ...]]:
    """Map each declared scenario to the semantic actions it proves."""
    result: dict[str, list[str]] = {}
    for spec in ACTION_SPECS:
        if spec.canary_scenario is not None:
            result.setdefault(spec.canary_scenario, []).append(str(spec.id))
    return {scenario_id: tuple(action_ids) for scenario_id, action_ids in sorted(result.items())}


def declared_canary_scenarios() -> dict[str, tuple[str, ...]]:
    """Return semantic-action and local security-assurance scenarios."""
    return {
        **declared_canary_actions(),
        **dict.fromkeys(SECURITY_CANARY_IDS, ()),
    }


def register_canary_scenario(scenario_id: str, scenario: CanaryScenario) -> None:
    """Register an executable scenario implementation from Pynchy or a plugin."""
    if scenario_id not in declared_canary_actions():
        raise ValueError(f"Canary scenario is not declared by an action: {scenario_id}")
    if scenario_id in _SCENARIO_EXECUTORS:
        raise ValueError(f"Canary scenario already registered: {scenario_id}")
    _SCENARIO_EXECUTORS[scenario_id] = scenario


def register_security_canary_scenario(scenario_id: str, scenario: CanaryScenario) -> None:
    """Register a declared local security-assurance scenario without action claims."""
    if scenario_id not in SECURITY_CANARY_IDS:
        raise ValueError(f"Security canary scenario is not declared: {scenario_id}")
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


async def run_declared_canaries(  # noqa: PLR0913 - optional revisions and executors support isolated runs.
    *,
    target_profile: str,
    scenario_ids: Collection[str] | None = None,
    scheduler_deps: object | None = None,
    executors: Mapping[str, CanaryScenario] | None = None,
    code_revision: str | None = None,
    config_revision: str | None = None,
) -> list[CanaryRun]:
    """Run selected declared scenarios and persist independently classified results."""
    scenario_actions = _selected_canary_actions(scenario_ids)
    active_executors = _SCENARIO_EXECUTORS if executors is None else executors
    revisions = (
        code_revision or _configured_runtime().code_revision(),
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
        results.append(await _configured_runtime().record_run(result))
    return results


def _selected_canary_actions(
    scenario_ids: Collection[str] | None,
) -> dict[str, tuple[str, ...]]:
    declared = declared_canary_scenarios()
    if scenario_ids is None:
        return declared
    unknown = sorted(set(scenario_ids) - set(declared))
    if unknown:
        raise ValueError(f"Unknown canary scenarios: {unknown}")
    return {scenario_id: declared[scenario_id] for scenario_id in scenario_ids}


async def get_canary_report(*, history_limit: int = 50) -> dict[str, object]:
    """Return declared coverage, recent history, and unresolved regressions."""
    runtime = _configured_runtime()
    latest, recent_runs, unresolved = await asyncio.gather(
        runtime.latest_runs(),
        runtime.recent_runs(history_limit),
        runtime.unresolved_regressions(),
    )
    latest_by_scenario: dict[str, list[dict[str, object]]] = {}
    for run in latest:
        latest_by_scenario.setdefault(run.scenario_id, []).append(_canary_run_dict(run))
    declared = declared_canary_scenarios()
    semantic_scenarios = declared_canary_actions()
    return {
        "summary": {
            "declared_scenarios": len(declared),
            "semantic_action_scenarios": len(semantic_scenarios),
            "security_assurance_scenarios": len(SECURITY_CANARY_IDS),
            "established_targets": sum(run.outcome is CanaryOutcome.PASSED for run in latest),
            "not_established_targets": sum(
                run.outcome is CanaryOutcome.NOT_ESTABLISHED for run in latest
            ),
            "unrun_scenarios": len(declared.keys() - latest_by_scenario.keys()),
            "unresolved_regressions": len(unresolved),
        },
        "scenarios": [
            {
                "id": scenario_id,
                "action_ids": action_ids,
                "evidence_kind": (
                    "semantic_action" if scenario_id in semantic_scenarios else "security_assurance"
                ),
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
    except Exception as exc:  # noqa: BLE001 - external canary failure must be redacted and persisted.
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
    except Exception as exc:  # noqa: BLE001 - verifier failure still requires artifact cleanup.
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
        except Exception as exc:  # noqa: BLE001 - cleanup gets independent retries before recording failure.
            error_class = type(exc).__name__
            logger.warning(
                "Canary cleanup failed",
                scenario_id=context.scenario_id,
                error_class=error_class,
            )
    return CanaryOutcome.CLEANUP_FAILED, error_class, ()


def _deduplicated_refs(refs: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def _config_revision() -> str:
    config_path = Path.cwd() / "data" / "personalization" / "pynchy.toml"
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
