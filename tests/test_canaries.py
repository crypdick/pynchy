"""Tests for external-service canary execution, durable evidence, and regressions."""

from __future__ import annotations

import pytest

from pynchy.canaries.api import (
    CanaryExercise,
    CanarySkippedError,
    declared_canary_actions,
    declared_canary_scenarios,
    get_canary_report,
    register_canary_scenario,
    register_security_canary_scenario,
    run_declared_canaries,
)
from pynchy.canary_contracts import CanaryOutcome
from pynchy.config.api import CanaryConfig, validate_settings_mapping
from pynchy.security_canary_ids import SECURITY_CANARY_IDS
from pynchy.state import (
    get_recent_canary_runs,
    get_unresolved_canary_regressions,
    init_test_database,
)

_SCENARIO_ID = "calendar.round.trip"


@pytest.mark.asyncio
async def test_runner_requires_configured_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pynchy.canaries._runner._runtime", None)

    with pytest.raises(RuntimeError, match="runtime has not been configured"):
        await run_declared_canaries(target_profile="canary-profile", scenario_ids=[])


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


class ExerciseFailureScenario(PassingScenario):
    async def exercise(self, _context):
        raise RuntimeError("provider unavailable")


class VerificationSkippedScenario(PassingScenario):
    async def verify(self, _context, _exercise):
        raise CanarySkippedError("verification temporarily unavailable")


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
    assert len(history) == len(declared_canary_scenarios())
    assert [run.scenario_id for run in await get_recent_canary_runs(scenario_id=_SCENARIO_ID)] == [
        _SCENARIO_ID
    ]
    report = await get_canary_report()
    assert report["summary"] == {
        "declared_scenarios": len(declared_canary_scenarios()),
        "semantic_action_scenarios": len(declared_canary_actions()),
        "security_assurance_scenarios": (
            len(declared_canary_scenarios()) - len(declared_canary_actions())
        ),
        "established_targets": 1,
        "not_established_targets": len(declared_canary_scenarios()) - 1,
        "unrun_scenarios": 0,
        "unresolved_regressions": 0,
    }


@pytest.mark.asyncio
async def test_report_counts_declared_scenarios_without_runs():
    await init_test_database()

    report = await get_canary_report()

    assert report["summary"]["unrun_scenarios"] == len(declared_canary_scenarios())


@pytest.mark.asyncio
async def test_runner_executes_only_the_configured_scenarios():
    await init_test_database()

    results = await run_declared_canaries(
        target_profile="canary-profile",
        scenario_ids=[_SCENARIO_ID],
        executors={_SCENARIO_ID: PassingScenario()},
    )

    assert [result.scenario_id for result in results] == [_SCENARIO_ID]


@pytest.mark.asyncio
async def test_runner_records_unknown_config_revision_when_config_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    await init_test_database()
    monkeypatch.chdir(tmp_path)

    results = await run_declared_canaries(
        target_profile="canary-profile",
        scenario_ids=[_SCENARIO_ID],
        executors={_SCENARIO_ID: PassingScenario()},
        code_revision="code-a",
    )

    assert results[0].config_revision == "unknown"


@pytest.mark.asyncio
async def test_runner_records_exercise_failure_without_provider_details():
    await init_test_database()

    results = await run_declared_canaries(
        target_profile="canary-profile",
        scenario_ids=[_SCENARIO_ID],
        executors={_SCENARIO_ID: ExerciseFailureScenario()},
    )

    result = results[0]
    assert result.outcome is CanaryOutcome.FAILED
    assert result.error_class == "RuntimeError"
    assert "provider unavailable" not in str(result)


@pytest.mark.asyncio
async def test_runner_records_verification_skip_with_exercise_evidence():
    await init_test_database()

    results = await run_declared_canaries(
        target_profile="canary-profile",
        scenario_ids=[_SCENARIO_ID],
        executors={_SCENARIO_ID: VerificationSkippedScenario()},
    )

    result = results[0]
    assert result.outcome is CanaryOutcome.SKIPPED
    assert result.evidence_refs == ("created:event-1", "deleted:event-1")


@pytest.mark.asyncio
async def test_runner_rejects_unknown_selected_scenario():
    await init_test_database()

    with pytest.raises(ValueError, match="Unknown canary scenarios"):
        await run_declared_canaries(
            target_profile="canary-profile",
            scenario_ids=["unknown.round.trip"],
        )


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


def test_enabled_canary_requires_explicit_scenario_selection():
    with pytest.raises(ValueError, match="scenario_ids is required"):
        validate_settings_mapping(
            {
                "profiles": {"external-canary": {}},
                "canary": {"enabled": True, "target_profile": "external-canary"},
            }
        )


def test_enabled_canary_rejects_unknown_scenarios():
    with pytest.raises(ValueError, match="includes unknown scenarios"):
        validate_settings_mapping(
            {
                "profiles": {"external-canary": {}},
                "canary": {
                    "enabled": True,
                    "target_profile": "external-canary",
                    "scenario_ids": ["unknown.round.trip"],
                },
            }
        )


def test_enabled_calendar_canary_requires_its_mcp_tool() -> None:
    with pytest.raises(ValueError, match="required tools: caldav"):
        validate_settings_mapping(
            {
                "profiles": {"external-canary": {}},
                "workspaces": {"canary": {}},
                "canary": {
                    "enabled": True,
                    "target_profile": "external-canary",
                    "scenario_ids": ["calendar.round.trip"],
                    "calendar_name": "canary-calendar",
                },
            }
        )


def test_enabled_computer_use_canary_requires_its_tool() -> None:
    with pytest.raises(ValueError, match="required tools: computer_use"):
        validate_settings_mapping(
            {
                "profiles": {"external-canary": {}},
                "canary": {
                    "enabled": True,
                    "target_profile": "external-canary",
                    "scenario_ids": ["desktop.computer.round.trip"],
                },
            }
        )


def test_enabled_linear_canary_requires_an_existing_workspace() -> None:
    with pytest.raises(ValueError, match="linear_workspace references unknown workspace"):
        validate_settings_mapping(
            {
                "profiles": {"external-canary": {"tools": ["linear"]}},
                "tools": {"linear": {"type": "linear"}},
                "canary": {
                    "enabled": True,
                    "target_profile": "external-canary",
                    "scenario_ids": ["linear.workspace.round.trip"],
                    "linear_team_key": "SYN",
                    "linear_workspace": "missing",
                },
            }
        )


def test_enabled_proton_round_trip_requires_a_delivery_recipient():
    with pytest.raises(ValueError, match="proton_recipient is required"):
        validate_settings_mapping(
            {
                "profiles": {"external-canary": {"tools": ["proton-mail"]}},
                "tools": {
                    "proton-mail": {
                        "type": "mcp",
                        "public_source": True,
                        "secret_data": True,
                        "public_sink": True,
                        "dangerous_writes": True,
                        "mcp": {
                            "runtime": "script",
                            "command": "uv",
                            "args": ["run", "proton-mail"],
                            "port": 8475,
                            "transport": "streamable_http",
                        },
                    }
                },
                "canary": {
                    "enabled": True,
                    "target_profile": "external-canary",
                    "scenario_ids": ["proton.mail.round.trip"],
                },
            }
        )


def test_enabled_google_drive_round_trip_requires_a_readable_fixture():
    with pytest.raises(ValueError, match="google_drive_file_id is required"):
        validate_settings_mapping(
            {
                "profiles": {"external-canary": {"tools": ["gdrive"]}},
                "tools": {
                    "gdrive": {
                        "type": "mcp",
                        "public_source": False,
                        "secret_data": True,
                        "public_sink": False,
                        "dangerous_writes": False,
                        "mcp": {
                            "runtime": "docker",
                            "image": "pynchy-mcp-gdrive:latest",
                            "port": 3100,
                            "transport": "streamable_http",
                        },
                    }
                },
                "canary": {
                    "enabled": True,
                    "target_profile": "external-canary",
                    "scenario_ids": ["drive.google.round.trip"],
                    "google_drive_server": "gdrive",
                    "google_drive_probe_query": "pynchy-canary-fixture",
                },
            }
        )


def test_canary_registration_rejects_a_scenario_without_action_coverage():
    with pytest.raises(ValueError, match="not declared"):
        register_canary_scenario("mail.send.self", PassingScenario())


def test_canary_registration_rejects_duplicate_scenarios(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pynchy.canaries._runner._SCENARIO_EXECUTORS",
        {_SCENARIO_ID: PassingScenario()},
    )

    with pytest.raises(ValueError, match="already registered"):
        register_canary_scenario(_SCENARIO_ID, PassingScenario())


def test_security_canary_registration_rejects_an_undeclared_scenario():
    with pytest.raises(ValueError, match="not declared"):
        register_security_canary_scenario("mail.send.self", PassingScenario())


def test_security_canary_registration_rejects_duplicate_scenarios(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_id = SECURITY_CANARY_IDS[0]
    monkeypatch.setattr(
        "pynchy.canaries._runner._SCENARIO_EXECUTORS",
        {scenario_id: PassingScenario()},
    )

    with pytest.raises(ValueError, match="already registered"):
        register_security_canary_scenario(scenario_id, PassingScenario())
