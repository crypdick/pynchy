"""Semantic agent-action catalog and hermetic coverage contract.

An action is one user-meaningful state transition, rather than a particular
tool implementation. The catalog gives tests, plugins, and future canaries a
stable identifier even when the IPC or MCP surface changes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable  # noqa: TC003, RUF100 - beartype resolves runtime annotations.
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

ActionId = NewType("ActionId", str)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+$")


class EvidenceRequirement(StrEnum):
    """Evidence required before an action is considered covered."""

    HERMETIC = "hermetic"
    HERMETIC_AND_AGENTIC = "hermetic_and_agentic"


@dataclass(frozen=True)
class ActionSpec:
    """Coverage contract for one semantic action.

    ``canary_scenario`` names the eventual real-service scenario. The
    hermetic gate validates that the scenario is declared, while the canary
    runner will later record whether it has actually passed.
    """

    id: ActionId
    owner: str
    summary: str
    test_requirement: EvidenceRequirement = EvidenceRequirement.HERMETIC
    canary_scenario: str | None = None


@dataclass(frozen=True)
class HermeticCoverageReport:
    """Result of comparing action specifications to collected test markers."""

    missing: tuple[str, ...]
    unknown: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return not self.missing and not self.unknown

    def describe(self) -> str:
        """Render an actionable error message for pytest or CI."""
        problems: list[str] = []
        if self.missing:
            problems.append(f"actions without hermetic tests: {', '.join(self.missing)}")
        if self.unknown:
            problems.append(f"tests mark unknown actions: {', '.join(self.unknown)}")
        return "; ".join(problems)


def validate_action_specs(specs: Iterable[ActionSpec]) -> tuple[str, ...]:
    """Return catalog errors without depending on pytest or plugin loading."""
    errors: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        action_id = str(spec.id)
        if not _IDENTIFIER_RE.fullmatch(action_id):
            errors.append(f"invalid action id: {action_id!r}")
        if action_id in seen:
            errors.append(f"duplicate action id: {action_id}")
        seen.add(action_id)
        if not spec.owner.strip():
            errors.append(f"{action_id}: owner is required")
        if not spec.summary.strip():
            errors.append(f"{action_id}: summary is required")
        if spec.test_requirement is EvidenceRequirement.HERMETIC_AND_AGENTIC:
            if spec.canary_scenario is None:
                errors.append(f"{action_id}: agentic coverage requires a canary scenario")
            elif not _IDENTIFIER_RE.fullmatch(spec.canary_scenario):
                errors.append(f"{action_id}: invalid canary scenario: {spec.canary_scenario!r}")
        elif spec.canary_scenario is not None:
            errors.append(f"{action_id}: hermetic-only action cannot declare a canary scenario")
    return tuple(errors)


def assess_hermetic_coverage(
    specs: Iterable[ActionSpec], marked_action_ids: Iterable[str]
) -> HermeticCoverageReport:
    """Compare the registered actions with ``pytest.mark.action`` declarations."""
    registered = {str(spec.id) for spec in specs}
    marked = set(marked_action_ids)
    return HermeticCoverageReport(
        missing=tuple(sorted(registered - marked)),
        unknown=tuple(sorted(marked - registered)),
    )


# NOTE: Update docs/architecture/action-coverage.md when changing action
# evidence requirements or the canary contract.
# The catalog contains action families with established behavioral tests.
# Action implementations must register here before the CI coverage gate can pass.
ACTION_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        ActionId("calendar.calendar.list"),
        "caldav",
        "Discover calendars available to the workspace.",
        EvidenceRequirement.HERMETIC_AND_AGENTIC,
        "calendar.round.trip",
    ),
    ActionSpec(
        ActionId("calendar.event.list"),
        "caldav",
        "List events in a calendar and date range.",
        EvidenceRequirement.HERMETIC_AND_AGENTIC,
        "calendar.round.trip",
    ),
    ActionSpec(
        ActionId("calendar.event.create"),
        "caldav",
        "Create an event in a selected calendar.",
        EvidenceRequirement.HERMETIC_AND_AGENTIC,
        "calendar.round.trip",
    ),
    ActionSpec(
        ActionId("calendar.event.delete"),
        "caldav",
        "Delete an event by identifier.",
        EvidenceRequirement.HERMETIC_AND_AGENTIC,
        "calendar.round.trip",
    ),
    ActionSpec(ActionId("memory.save"), "sqlite-memory", "Create or update a memory."),
    ActionSpec(ActionId("memory.recall"), "sqlite-memory", "Retrieve relevant memories."),
    ActionSpec(ActionId("memory.forget"), "sqlite-memory", "Delete a memory."),
    ActionSpec(ActionId("memory.list"), "sqlite-memory", "List memories in a workspace."),
    ActionSpec(ActionId("task.schedule"), "agent-tools", "Create a scheduled agent task."),
    ActionSpec(ActionId("task.list"), "agent-tools", "List scheduled tasks."),
    ActionSpec(ActionId("task.pause"), "agent-tools", "Pause a scheduled task."),
    ActionSpec(ActionId("task.resume"), "agent-tools", "Resume a scheduled task."),
    ActionSpec(ActionId("task.cancel"), "agent-tools", "Cancel a scheduled task."),
    ActionSpec(ActionId("todo.list"), "agent-tools", "List workspace todos."),
    ActionSpec(ActionId("todo.complete"), "agent-tools", "Mark a todo complete."),
    ActionSpec(ActionId("message.outbound.queue"), "agent-tools", "Queue an outbound message."),
    ActionSpec(
        ActionId("message.outbound.retry"),
        "messaging",
        "Retry an undelivered outbound message.",
        EvidenceRequirement.HERMETIC_AND_AGENTIC,
        "channel.outbound.round.trip",
    ),
    ActionSpec(
        ActionId("user.question.ask"),
        "messaging",
        "Ask a user and route their answer back to the agent.",
        EvidenceRequirement.HERMETIC_AND_AGENTIC,
        "channel.ask.answer",
    ),
)

_CATALOG_ERRORS = validate_action_specs(ACTION_SPECS)
if _CATALOG_ERRORS:
    raise RuntimeError(f"Invalid built-in action catalog: {'; '.join(_CATALOG_ERRORS)}")
