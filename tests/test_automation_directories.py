"""Behavioral coverage for directory-scoped automations."""

from pathlib import Path

import pytest

from pynchy.config.api import PersonalizationError, load_layered_settings_mapping


def _automation_directory(tmp_path: Path, name: str) -> tuple[Path, Path]:
    defaults = tmp_path / "data" / "defaults"
    personalization = tmp_path / "data" / "personalization"
    directory = personalization / "automations" / name
    defaults.mkdir(parents=True)
    directory.mkdir(parents=True)
    (defaults / "pynchy.toml").write_text("", encoding="utf-8")
    return personalization, directory


def test_directory_automation_resolves_command_working_directory(tmp_path: Path) -> None:
    personalization, directory = _automation_directory(tmp_path, "weekly")
    working_directory = tmp_path / "external"
    (directory / "config.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 9 * * 1"\n'
        'workspace = "host"\ncommand = "./scripts/run.py"\n'
        f'cwd = "{working_directory}"\n',
        encoding="utf-8",
    )

    mapping = load_layered_settings_mapping(tmp_path, personalization_root=personalization)

    assert mapping["jobs"]["weekly"]["cwd"] == str(working_directory)


def test_rejects_hidden_automation_directory(tmp_path: Path) -> None:
    personalization, directory = _automation_directory(tmp_path, ".hidden")
    (directory / "config.toml").write_text(
        'schema_version = 1\n[job]\nschedule = "0 9 * * 1"\n'
        'workspace = "pynchy"\nprompt = "hidden"\n',
        encoding="utf-8",
    )

    with pytest.raises(PersonalizationError, match="Invalid automation name"):
        load_layered_settings_mapping(tmp_path, personalization_root=personalization)
