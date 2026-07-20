"""Operational evidence tests for harmless security-assurance canaries."""

from __future__ import annotations

import pytest

from pynchy.canaries import get_canary_report, run_declared_canaries
from pynchy.config import validate_settings_mapping
from pynchy.security_canary_ids import SECURITY_CANARY_IDS
from pynchy.state import init_test_database
from pynchy.types import CanaryOutcome


@pytest.mark.asyncio
async def test_security_canaries_run_through_existing_durable_runner() -> None:
    await init_test_database()

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
