"""Tests for external-service canary execution, durable evidence, and regressions."""

from __future__ import annotations

import pytest

from pynchy.canaries import (
    CanaryExercise,
    CanarySkippedError,
    declared_canary_actions,
    get_canary_report,
    register_canary_scenario,
    run_declared_canaries,
)
from pynchy.config import CanaryConfig, validate_settings_mapping
from pynchy.state import (
    get_recent_canary_runs,
    get_unresolved_canary_regressions,
    init_test_database,
)
from pynchy.types import CanaryOutcome

_SCENARIO_ID = "calendar.round.trip"


class PassingScenario:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def exercise(self, _context):
        self.calls.append("exercise")
        return CanaryExercise(artifact="calendar-event", evidence_refs=("created:event-1",))

    async def verify(self, _context, exercise):
        self.calls.append(f"verify:{exercise.artifact}")
        return ("verified:event-1",)

    async def cleanup(self, _context, exercise):
        self.calls.append(f"cleanup:{exercise.artifact}")
        return ("deleted:event-1",)


class VerificationFailureScenario(PassingScenario):
    async def verify(self, _context, exercise):
        self.calls.append(f"verify:{exercise.artifact}")
        raise RuntimeError("provider response must never reach the report")


class CleanupFailureScenario(PassingScenario):
    async def cleanup(self, _context, exercise):
        self.calls.append(f"cleanup:{exercise.artifact}")
        raise RuntimeError("cleanup request failed")


class SkippedScenario:
    async def exercise(self, _context):
        raise CanarySkippedError("test identity temporarily unavailable")

    async def verify(self, _context, _exercise):
        raise AssertionError("skip before verification")

    async def cleanup(self, _context, _exercise):
        raise AssertionError("skip before cleanup")


@pytest.mark.asyncio
async def test_runner_records_evidence_and_marks_unimplemented_scenarios_not_established():
    await init_test_database()
    scenario = PassingScenario()

    results = await run_declared_canaries(
        target_profile="canary-profile",
        executors={_SCENARIO_ID: scenario},
        code_revision="code-a",
        config_revision="config-a",
    )

    by_id = {result.scenario_id: result for result in results}
    calendar = by_id[_SCENARIO_ID]
    assert calendar.outcome is CanaryOutcome.PASSED
    assert calendar.evidence_refs == ("created:event-1", "verified:event-1", "deleted:event-1")
    assert scenario.calls == ["exercise", "verify:calendar-event", "cleanup:calendar-event"]
    assert all(
        result.outcome is CanaryOutcome.NOT_ESTABLISHED
        for scenario_id, result in by_id.items()
        if scenario_id != _SCENARIO_ID
    )

    history = await get_recent_canary_runs(limit=20)
    assert len(history) == len(declared_canary_actions())
    report = await get_canary_report()
    assert report["summary"] == {
        "declared_scenarios": len(declared_canary_actions()),
        "established_targets": 1,
        "not_established_targets": len(declared_canary_actions()) - 1,
        "unresolved_regressions": 0,
    }


@pytest.mark.asyncio
async def test_failure_after_success_becomes_regression_then_records_recovery():
    await init_test_database()

    await run_declared_canaries(
        target_profile="canary-profile",
        executors={_SCENARIO_ID: PassingScenario()},
    )
    failed = await run_declared_canaries(
        target_profile="canary-profile",
        executors={_SCENARIO_ID: VerificationFailureScenario()},
    )

    regression = next(result for result in failed if result.scenario_id == _SCENARIO_ID)
    assert regression.outcome is CanaryOutcome.FAILED
    assert regression.error_class == "RuntimeError"
    assert regression.is_regression is True
    assert regression.starts_regression is True
    assert "provider response" not in str(regression)
    assert [run.scenario_id for run in await get_unresolved_canary_regressions()] == [_SCENARIO_ID]

    skipped = await run_declared_canaries(
        target_profile="canary-profile",
        executors={_SCENARIO_ID: SkippedScenario()},
    )
    skipped_result = next(result for result in skipped if result.scenario_id == _SCENARIO_ID)
    assert skipped_result.outcome is CanaryOutcome.SKIPPED
    assert skipped_result.is_regression is True
    assert [run.scenario_id for run in await get_unresolved_canary_regressions()] == [_SCENARIO_ID]

    recovered = await run_declared_canaries(
        target_profile="canary-profile",
        executors={_SCENARIO_ID: PassingScenario()},
    )

    recovery = next(result for result in recovered if result.scenario_id == _SCENARIO_ID)
    assert recovery.outcome is CanaryOutcome.PASSED
    assert recovery.is_recovery is True
    assert await get_unresolved_canary_regressions() == []


@pytest.mark.asyncio
async def test_cleanup_retries_and_overrides_success_with_cleanup_failure():
    await init_test_database()
    scenario = CleanupFailureScenario()

    results = await run_declared_canaries(
        target_profile="canary-profile",
        executors={_SCENARIO_ID: scenario},
    )

    result = next(item for item in results if item.scenario_id == _SCENARIO_ID)
    assert result.outcome is CanaryOutcome.CLEANUP_FAILED
    assert result.error_class == "RuntimeError"
    assert scenario.calls == [
        "exercise",
        "verify:calendar-event",
        "cleanup:calendar-event",
        "cleanup:calendar-event",
        "cleanup:calendar-event",
    ]


def test_canary_config_requires_a_dedicated_target_when_enabled():
    with pytest.raises(ValueError, match="target_profile is required"):
        CanaryConfig(enabled=True)


def test_enabled_canary_target_must_reference_a_configured_profile():
    with pytest.raises(ValueError, match="references unknown profile"):
        validate_settings_mapping(
            {"canary": {"enabled": True, "target_profile": "external-canary"}}
        )


def test_canary_registration_rejects_a_scenario_without_action_coverage():
    with pytest.raises(ValueError, match="not declared"):
        register_canary_scenario("mail.send.self", PassingScenario())
