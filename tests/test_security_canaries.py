"""Operational evidence tests for harmless security-assurance canaries."""

from __future__ import annotations

from dataclasses import replace

import pytest
from conftest import make_settings

from pynchy.canaries.api import (
    CanaryExercise,
    CanaryRunContext,
    get_canary_report,
    registered_canary_scenarios,
    run_declared_canaries,
)
from pynchy.canary_contracts import CanaryOutcome
from pynchy.config.api import validate_settings_mapping
from pynchy.host.orchestrator.plugin_configuration import configure_builtin_canaries
from pynchy.security_canary_ids import SECURITY_CANARY_IDS
from pynchy.state import init_test_database


@pytest.mark.asyncio
async def test_security_canaries_run_through_existing_durable_runner() -> None:
    await init_test_database()
    configure_builtin_canaries(make_settings())

    results = await run_declared_canaries(
        target_profile="local-security-assurance",
        scenario_ids=SECURITY_CANARY_IDS,
        code_revision="security-code",
        config_revision="security-config",
    )

    assert [result.scenario_id for result in results] == list(SECURITY_CANARY_IDS)
    assert all(result.outcome is CanaryOutcome.PASSED for result in results)
    assert all(result.action_ids == () for result in results)
    assert all(
        evidence.startswith("security:") for result in results for evidence in result.evidence_refs
    )
    assert "canary-secret-value" not in str(results)

    report = await get_canary_report()
    security_rows = [
        scenario for scenario in report["scenarios"] if scenario["id"] in SECURITY_CANARY_IDS
    ]
    assert len(security_rows) == len(SECURITY_CANARY_IDS)
    assert all(row["evidence_kind"] == "security_assurance" for row in security_rows)
    assert all(row["action_ids"] == () for row in security_rows)


@pytest.mark.asyncio
async def test_security_canary_verifiers_reject_invalid_artifacts() -> None:
    configure_builtin_canaries(make_settings())
    scenarios = registered_canary_scenarios()
    context = CanaryRunContext("run", "security", "local-security-assurance", None)

    deterministic = scenarios["security.deterministic.hard-block"]
    exercise = await deterministic.exercise(context)
    with pytest.raises(RuntimeError, match="Unexpected deterministic"):
        await deterministic.verify(context, CanaryExercise(None))
    with pytest.raises(RuntimeError, match="did not deny"):
        await deterministic.verify(
            context, replace(exercise, artifact=replace(exercise.artifact, decision="allow"))
        )

    expected_failures = {
        "security.approval.mutation-replay": "Approval binding",
        "security.cop.degraded-approval": "Degraded Cop",
        "security.gateway.posture": "Gateway redaction",
        "security.package.metadata": "Package metadata",
    }
    for scenario_id, message in expected_failures.items():
        scenario = scenarios[scenario_id]
        with pytest.raises(RuntimeError, match=message):
            await scenario.verify(context, CanaryExercise(None))


def test_security_canaries_are_valid_explicit_schedule_selections() -> None:
    settings = validate_settings_mapping(
        {
            "profiles": {"security-canary": {}},
            "canary": {
                "enabled": True,
                "target_profile": "security-canary",
                "scenario_ids": list(SECURITY_CANARY_IDS),
            },
        }
    )

    assert settings.canary.scenario_ids == list(SECURITY_CANARY_IDS)
