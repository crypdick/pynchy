"""Public scheduled-work health classification."""

from pynchy.scheduling.api import scheduled_work_health_reasons


def test_classifies_only_semantic_scheduled_work_failures() -> None:
    def health_reasons(**overrides: object) -> tuple[str, ...]:
        values: dict[str, object] = {
            "status": "active",
            "next_run": "2026-08-05T23:00:00+00:00",
            "consecutive_failures": 0,
            "orchestration_error": None,
            "last_result": None,
        }
        values.update(overrides)
        return scheduled_work_health_reasons(**values)  # type: ignore[arg-type]

    assert (
        health_reasons(
            last_result="Completed with 0 failures and no errors",
        )
        == ()
    )
    assert (
        health_reasons(
            last_result='{"wakeAgent": false}',
        )
        == ()
    )
    assert health_reasons(consecutive_failures=1) == ("recent_failure",)
    assert health_reasons(
        last_result="Blocked: missing credentials",
    ) == ("failure_shaped_result",)
    assert health_reasons(
        orchestration_error="stale Temporal failure",
    ) == ("scheduler_error",)
