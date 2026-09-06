"""Atomic publication contracts for workspace configuration documents."""

from __future__ import annotations

import tomllib
from unittest.mock import patch

import pytest

from pynchy.config.api import JobConfig
from pynchy.host.orchestrator.automation_config import (
    add_job_to_toml,
    delete_automation_toml,
    update_automation_toml,
)


def test_add_job_writes_a_versioned_automation_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    add_job_to_toml(
        "nightly",
        JobConfig(
            schedule="0 * * * *",
            workspace="host",
            command="true",
        ).model_dump(exclude_none=True, exclude_defaults=True),
    )

    config = tmp_path / "data/personalization/automations/nightly/config.toml"
    data = tomllib.loads(config.read_text())
    assert data["schema_version"] == 1
    assert data["job"] == {"schedule": "0 * * * *", "workspace": "host", "command": "true"}


def test_automation_definition_can_update_and_delete(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    add_job_to_toml(
        "nightly",
        JobConfig(
            schedule="0 * * * *",
            workspace="host",
            command="true",
        ).model_dump(exclude_none=True, exclude_defaults=True),
    )

    update_automation_toml(
        "nightly",
        {"enabled": False},
        parse_and_dump=lambda fields: JobConfig.model_validate(fields).model_dump(
            exclude_none=True,
            exclude_defaults=True,
        ),
    )

    config = tmp_path / "data/personalization/automations/nightly/config.toml"
    assert tomllib.loads(config.read_text())["job"]["enabled"] is False
    delete_automation_toml("nightly")
    assert not config.exists()


@pytest.mark.parametrize("name", ["", ".hidden", "nested/name"])
def test_automation_definition_rejects_unsafe_names(tmp_path, name) -> None:
    with pytest.raises(ValueError, match="Invalid automation name"):
        add_job_to_toml(name, {"schedule": "0 * * * *"}, project_root=tmp_path)


def test_automation_definition_requires_a_valid_existing_job(tmp_path) -> None:
    with pytest.raises(ValueError, match="Automation not found"):
        update_automation_toml(
            "missing",
            {},
            parse_and_dump=lambda fields: fields,
            project_root=tmp_path,
        )
    with pytest.raises(ValueError, match="Automation not found"):
        delete_automation_toml("missing", project_root=tmp_path)

    config = tmp_path / "data/personalization/automations/broken/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text("schema_version = 1\n", encoding="utf-8")
    with pytest.raises(TypeError, match="Invalid automation definition"):
        update_automation_toml(
            "broken",
            {},
            parse_and_dump=lambda fields: fields,
            project_root=tmp_path,
        )


def test_add_job_preserves_existing_file_when_publish_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    automation_path = tmp_path / "data/personalization/automations/nightly/config.toml"
    automation_path.parent.mkdir(parents=True)
    original = 'schema_version = 1\n[job]\nschedule = "old"\n'
    automation_path.write_text(original, encoding="utf-8")

    with (
        patch("pynchy.atomic_json.os.replace", side_effect=OSError("publish failed")),
        pytest.raises(OSError, match="publish failed"),
    ):
        add_job_to_toml(
            "nightly",
            JobConfig(
                schedule="0 * * * *",
                workspace="host",
                command="true",
            ).model_dump(exclude_none=True, exclude_defaults=True),
        )

    assert automation_path.read_text(encoding="utf-8") == original
