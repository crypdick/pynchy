"""Pydantic settings-source protocol contracts."""

from __future__ import annotations

from pynchy.config.api import Settings
from pynchy.config.settings_sources import PersonalizationSettingsSource


def test_personalization_source_returns_field_value_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        "pynchy.config.settings_sources.load_layered_settings_mapping",
        lambda _project_root: {"server": {"host": "example.test"}},
    )
    source = PersonalizationSettingsSource(Settings)

    value, field_name, is_complex = source.get_field_value(
        Settings.model_fields["server"], "server"
    )

    assert value == {"host": "example.test"}
    assert field_name == "server"
    assert is_complex is False
    assert source() is source()
