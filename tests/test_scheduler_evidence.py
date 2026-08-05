"""Public state behavior for bounded scheduler audit evidence."""

from __future__ import annotations

import pytest

from pynchy.scheduling.api import (
    SchedulerAuditClassification,
    SchedulerDefinition,
    SchedulerEvidenceOutcome,
    SchedulerOccurrence,
)
from pynchy.state.api import (
    audit_scheduler_evidence,
    get_scheduler_occurrences,
    init_test_database,
    prune_scheduler_evidence,
    record_scheduler_occurrence,
    register_scheduler_definition,
    scheduler_definition_hash,
)


@pytest.fixture(autouse=True)
async def _setup_db() -> None:
    await init_test_database()


class TestSchedulerEvidence:
    async def test_audit_classifies_terminal_and_missing_interval_slots(self) -> None:
        definition = SchedulerDefinition(
            schedule_key="task:daily-review",
            schedule_type="interval",
            schedule_value="5",
            timezone="UTC",
            active_from="2026-08-05T00:00:00+00:00",
            definition_hash=scheduler_definition_hash("task:daily-review", "interval", "5", "UTC"),
        )
        await register_scheduler_definition(definition)
        await record_scheduler_occurrence(
            SchedulerOccurrence(
                definition_hash=definition.definition_hash,
                scheduled_at="2026-08-05T00:00:00+00:00",
                outcome=SchedulerEvidenceOutcome.SUCCEEDED,
                dispatched_at="2026-08-05T00:00:00+00:00",
                terminal_at="2026-08-05T00:00:01+00:00",
            )
        )
        await record_scheduler_occurrence(
            SchedulerOccurrence(
                definition_hash=definition.definition_hash,
                scheduled_at="2026-08-05T00:05:00+00:00",
                outcome=SchedulerEvidenceOutcome.GATE_SKIPPED,
                reason="wakeAgent=false",
            )
        )

        slots = await audit_scheduler_evidence(
            start_at="2026-08-05T00:00:00+00:00",
            end_at="2026-08-05T00:10:00+00:00",
            now="2026-08-05T00:20:00+00:00",
            missed_after_seconds=60,
        )

        assert [slot.classification for slot in slots] == [
            SchedulerAuditClassification.RUN,
            SchedulerAuditClassification.DECLARED_SKIP,
            SchedulerAuditClassification.MISSED,
        ]

    async def test_pruning_advances_gap_watermark(self) -> None:
        definition = SchedulerDefinition(
            schedule_key="host:sync",
            schedule_type="interval",
            schedule_value="5",
            timezone="UTC",
            active_from="2026-08-05T00:00:00+00:00",
            definition_hash=scheduler_definition_hash("host:sync", "interval", "5", "UTC"),
        )
        await register_scheduler_definition(definition)
        occurrence = SchedulerOccurrence(
            definition_hash=definition.definition_hash,
            scheduled_at="2026-08-05T00:00:00+00:00",
            outcome=SchedulerEvidenceOutcome.SUCCEEDED,
            terminal_at="2026-08-05T00:00:01+00:00",
        )
        await record_scheduler_occurrence(occurrence)

        assert await prune_scheduler_evidence(before="2026-08-05T00:01:00+00:00") == 1
        assert not await get_scheduler_occurrences(
            definition.definition_hash,
            start_at="2026-08-05T00:00:00+00:00",
            end_at="2026-08-05T00:01:00+00:00",
        )
        slots = await audit_scheduler_evidence(
            start_at="2026-08-05T00:00:00+00:00",
            end_at="2026-08-05T00:00:00+00:00",
            now="2026-08-05T00:20:00+00:00",
            missed_after_seconds=60,
        )

        assert slots[0].classification is SchedulerAuditClassification.RETENTION_GAP
