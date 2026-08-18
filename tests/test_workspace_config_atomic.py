"""Atomic publication contracts for workspace configuration documents."""

from __future__ import annotations

import tomllib
from unittest.mock import patch

import pytest
from conftest import make_settings

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config.api import JobConfig, WorkspaceConfig
from pynchy.host.orchestrator.workspace_config import add_job_to_toml, add_workspace_to_toml


def test_add_job_writes_a_versioned_automation_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    add_job_to_toml(
        "nightly",
        JobConfig(schedule="0 * * * *", workspace="host", command="true"),
    )

    config = tmp_path / "data/personalization/automations/nightly/config.toml"
    data = tomllib.loads(config.read_text())
    assert data["schema_version"] == 1
    assert data["job"] == {"schedule": "0 * * * *", "workspace": "host", "command": "true"}


def test_add_workspace_preserves_existing_file_when_publish_fails(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    workspace_path = tmp_path / "data/personalization/workspaces/daily.toml"
    workspace_path.parent.mkdir(parents=True)
    original = "schema_version = 1\n[workspace]\n"
    workspace_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(workspace_config, "get_settings", make_settings)

    with (
        patch("pynchy.atomic_json.os.replace", side_effect=OSError("publish failed")),
        pytest.raises(OSError, match="publish failed"),
    ):
        add_workspace_to_toml("daily", WorkspaceConfig())

    assert workspace_path.read_text(encoding="utf-8") == original


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
            JobConfig(schedule="0 * * * *", workspace="host", command="true"),
        )

    assert automation_path.read_text(encoding="utf-8") == original
