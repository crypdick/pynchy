"""Pydantic settings-source protocol contracts."""

from __future__ import annotations

from pynchy.config.api import Settings
from pynchy.config.settings_sources import PersonalizationSettingsSource


def test_personalization_source_returns_field_value_contract() -> None:
    source = PersonalizationSettingsSource(Settings)

    value, field_name, is_complex = source.get_field_value(
        Settings.model_fields["server"], "server"
    )

    assert value is None
    assert field_name == "server"
    assert is_complex is False
