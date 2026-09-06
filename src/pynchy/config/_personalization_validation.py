"""Complete validation for a personalization configuration tree."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pynchy.config.personalization import (
    LITELLM_FILENAME,
    validate_litellm_model_names,
    validate_personalization_tree,
)

if TYPE_CHECKING:
    from pynchy.config.settings import Settings
else:
    Settings = Any


def validate_personalization_configuration(
    project_root: Path,
    personalization_root: Path,
) -> Settings:
    """Fully validate a personalization tree before runtime use or publication."""
    from pynchy.config.settings import (  # noqa: PLC0415 - settings imports the tree loader through its source adapter.
        validate_settings_mapping,
    )

    mapping = validate_personalization_tree(project_root, personalization_root)
    settings = validate_settings_mapping(mapping)
    validate_litellm_model_names(
        personalization_root / LITELLM_FILENAME,
        settings.configured_agent_models(),
    )
    return settings
