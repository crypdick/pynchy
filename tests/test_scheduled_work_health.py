"""Public scheduled-work attention classification."""

from pynchy.scheduling.api import ScheduledWorkHealth, scheduled_work_attention


def test_classifies_only_semantic_scheduled_work_failures() -> None:
    def attention(**overrides: object) -> tuple[str, ...]:
        values: dict[str, object] = {
            "status": "active",
            "next_run": "2026-08-05T23:00:00+00:00",
            "last_run_status": "success",
            "consecutive_failures": 0,
            "orchestration_error": None,
            "last_result": None,
        }
        values.update(overrides)
        return scheduled_work_attention(ScheduledWorkHealth(**values))  # type: ignore[arg-type]

    assert (
        attention(
            last_result="Completed with 0 failures and no errors",
        )
        == ()
    )
    assert (
        attention(
            last_result='{"wakeAgent": false}',
        )
        == ()
    )
    assert attention(
        last_run_status="error",
        consecutive_failures=1,
    ) == ("recent_failure",)
    assert attention(
        last_result="Blocked: missing credentials",
    ) == ("failure_shaped_result",)
    assert attention(
        orchestration_error="stale Temporal failure",
    ) == ("scheduler_error",)
