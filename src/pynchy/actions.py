"""Public semantic action catalog and hermetic coverage contract."""

from __future__ import annotations

from pynchy._action_contract import (
    ActionId,
    ActionSpec,
    ActionSurface,
    ActionTransport,
    EvidenceRequirement,
    HermeticCoverageReport,
    assess_hermetic_coverage,
    validate_action_specs,
)
from pynchy._action_specs import ACTION_SPECS

__all__ = [
    "ACTION_SPECS",
    "ActionId",
    "ActionSpec",
    "ActionSurface",
    "ActionTransport",
    "EvidenceRequirement",
    "HermeticCoverageReport",
    "assess_hermetic_coverage",
    "validate_action_specs",
]

_CATALOG_ERRORS = validate_action_specs(ACTION_SPECS)
if _CATALOG_ERRORS:
    raise RuntimeError(f"Invalid built-in action catalog: {'; '.join(_CATALOG_ERRORS)}")
