"""Domain contracts for durable operational assurance checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CanaryRunContext:
    """Context supplied to one scenario's exercise, verification, and cleanup."""

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

    async def exercise(self, context: CanaryRunContext) -> CanaryExercise: ...

    async def verify(
        self, context: CanaryRunContext, exercise: CanaryExercise
    ) -> tuple[str, ...]: ...

    async def cleanup(
        self, context: CanaryRunContext, exercise: CanaryExercise
    ) -> tuple[str, ...]: ...


class CanarySkippedError(RuntimeError):
    """Signal that a configured scenario cannot run without failing it."""
