"""Durable history and regression state for external-service canaries."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiosqlite import Row
else:
    Row = Any

from pynchy.state.connection import _get_db, atomic_write
from pynchy.types import CanaryOutcome, CanaryRun

_REGRESSION_OUTCOMES = frozenset({CanaryOutcome.FAILED, CanaryOutcome.CLEANUP_FAILED})


def _row_to_canary_run(row: Row) -> CanaryRun:
    return CanaryRun(
        run_id=row["run_id"],
        scenario_id=row["scenario_id"],
        action_ids=tuple(json.loads(row["action_ids"])),
        target_profile=row["target_profile"],
        code_revision=row["code_revision"],
        config_revision=row["config_revision"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        outcome=CanaryOutcome(row["outcome"]),
        error_class=row["error_class"],
        evidence_refs=tuple(json.loads(row["evidence_refs"])),
        is_regression=bool(row["is_regression"]),
        starts_regression=bool(row["starts_regression"]),
        is_recovery=bool(row["is_recovery"]),
    )


async def record_canary_run(run: CanaryRun) -> CanaryRun:
    """Persist one run and classify regression/recovery from stored evidence."""
    async with atomic_write() as db:
        cursor = await db.execute(
            """
            SELECT outcome, is_regression
            FROM canary_runs
            WHERE scenario_id = ? AND target_profile = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (run.scenario_id, run.target_profile),
        )
        last_row = await cursor.fetchone()
        cursor = await db.execute(
            """
            SELECT 1
            FROM canary_runs
            WHERE scenario_id = ? AND target_profile = ? AND outcome = ?
            LIMIT 1
            """,
            (run.scenario_id, run.target_profile, CanaryOutcome.PASSED.value),
        )
        had_prior_success = await cursor.fetchone() is not None
        last_is_regression = bool(last_row and last_row["is_regression"])
        is_failure = run.outcome in _REGRESSION_OUTCOMES
        remains_unresolved = last_is_regression and run.outcome is not CanaryOutcome.PASSED
        classified_run = replace(
            run,
            is_regression=(is_failure and had_prior_success) or remains_unresolved,
            starts_regression=is_failure and had_prior_success and not last_is_regression,
            is_recovery=run.outcome is CanaryOutcome.PASSED and last_is_regression,
        )
        await db.execute(
            """
            INSERT INTO canary_runs (
                run_id, scenario_id, action_ids, target_profile, code_revision,
                config_revision, started_at, completed_at, outcome, error_class,
                evidence_refs, is_regression, starts_regression, is_recovery
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                classified_run.run_id,
                classified_run.scenario_id,
                json.dumps(classified_run.action_ids),
                classified_run.target_profile,
                classified_run.code_revision,
                classified_run.config_revision,
                classified_run.started_at,
                classified_run.completed_at,
                classified_run.outcome.value,
                classified_run.error_class,
                json.dumps(classified_run.evidence_refs),
                int(classified_run.is_regression),
                int(classified_run.starts_regression),
                int(classified_run.is_recovery),
            ),
        )
    return classified_run


async def get_recent_canary_runs(
    *, limit: int = 50, scenario_id: str | None = None
) -> list[CanaryRun]:
    """Return newest canary results, optionally for one declared scenario."""
    db = _get_db()
    if scenario_id is None:
        cursor = await db.execute(
            "SELECT * FROM canary_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM canary_runs WHERE scenario_id = ? ORDER BY id DESC LIMIT ?",
            (scenario_id, limit),
        )
    return [_row_to_canary_run(row) for row in await cursor.fetchall()]


async def get_unresolved_canary_regressions() -> list[CanaryRun]:
    """Return scenarios whose latest result remains a post-success failure."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT runs.*
        FROM canary_runs AS runs
        INNER JOIN (
            SELECT scenario_id, target_profile, MAX(id) AS latest_id
            FROM canary_runs
            GROUP BY scenario_id, target_profile
        ) AS latest ON latest.latest_id = runs.id
        WHERE runs.is_regression = 1
        ORDER BY runs.id DESC
        """
    )
    return [_row_to_canary_run(row) for row in await cursor.fetchall()]


async def get_latest_canary_runs() -> list[CanaryRun]:
    """Return the current result for every scenario and target profile."""
    db = _get_db()
    cursor = await db.execute(
        """
        SELECT runs.*
        FROM canary_runs AS runs
        INNER JOIN (
            SELECT scenario_id, target_profile, MAX(id) AS latest_id
            FROM canary_runs
            GROUP BY scenario_id, target_profile
        ) AS latest ON latest.latest_id = runs.id
        ORDER BY runs.scenario_id, runs.target_profile
        """
    )
    return [_row_to_canary_run(row) for row in await cursor.fetchall()]
