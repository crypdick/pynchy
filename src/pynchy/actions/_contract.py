"""Types and validation for the semantic action coverage contract."""

from __future__ import annotations

import re
from collections.abc import Iterable  # noqa: TC003 - beartype resolves runtime annotations.
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType

ActionId = NewType("ActionId", str)

_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)+$")


class EvidenceRequirement(StrEnum):
    """Evidence required before an action is considered covered."""

    HERMETIC = "hermetic"
    HERMETIC_AND_AGENTIC = "hermetic_and_agentic"


class ActionTransport(StrEnum):
    """Pynchy boundary where an agent invokes an action."""

    AGENT_TOOL = "agent_tool"
    MCP_TOOL = "mcp_tool"
    HOST_WORKFLOW = "host_workflow"


@dataclass(frozen=True)
class ActionSurface:
    """One tool or host workflow that realizes a semantic action.

    ``operation`` disambiguates actions multiplexed through one tool, such as
    the individual computer-use commands. ``name`` remains the stable tool
    name exposed to an agent or MCP client.
    """

    transport: ActionTransport
    name: str
    operation: str | None = None


@dataclass(frozen=True)
class ActionSpec:
    """Coverage contract for one semantic action."""

    id: ActionId
    owner: str
    summary: str
    test_requirement: EvidenceRequirement = EvidenceRequirement.HERMETIC
    canary_scenario: str | None = None
    surfaces: tuple[ActionSurface, ...] = ()


@dataclass(frozen=True)
class HermeticCoverageReport:
    """Result of comparing action specifications to collected test markers."""

    missing: tuple[str, ...]
    unknown: tuple[str, ...]

    @property  # noqa: V106
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
        for surface in spec.surfaces:
            if not surface.name.strip():
                errors.append(f"{action_id}: action surface name is required")
            if surface.operation is not None and not surface.operation.strip():
                errors.append(f"{action_id}: action surface operation cannot be blank")
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
