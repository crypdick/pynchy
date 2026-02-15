"""Tests for workspace configuration.

Tests the unified YAML loading, validation, and writing logic for workspace.yaml
files. This covers both workspace identity (is_god, requires_trigger) and optional
periodic scheduling — critical business logic where incorrect parsing could cause
groups to launch with wrong permissions or agents to miss schedules.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from pynchy.workspace_config import (
    WorkspaceConfig,
    get_project_access_folders,
    has_project_access,
    load_workspace_config,
    write_workspace_config,
)


def _write_yaml(tmp_path, group_folder, data):
    """Helper to write workspace.yaml for a group folder."""
    config_path = tmp_path / group_folder / "workspace.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        config_path.write_text("")
    else:
        config_path.write_text(yaml.dump(data, default_flow_style=False))
    return config_path


class TestLoadWorkspaceConfig:
    """Test the load_workspace_config() function which parses workspace.yaml files."""

    def test_loads_periodic_config(self, tmp_path, monkeypatch):
        """Valid periodic config with schedule+prompt should load as periodic."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(
            tmp_path,
            "test-group",
            {
                "schedule": "0 9 * * *",
                "prompt": "Check for updates",
            },
        )

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.schedule == "0 9 * * *"
        assert result.prompt == "Check for updates"
        assert result.context_mode == "group"
        assert result.project_access is False
        assert result.is_periodic is True

    def test_loads_config_with_all_fields(self, tmp_path, monkeypatch):
        """Config with all fields should load correctly."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(
            tmp_path,
            "test-group",
            {
                "is_god": True,
                "requires_trigger": False,
                "project_access": True,
                "name": "Custom Name",
                "schedule": "*/5 * * * *",
                "prompt": "Monitor system",
                "context_mode": "isolated",
            },
        )

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.is_god is True
        assert result.requires_trigger is False
        assert result.project_access is True
        assert result.name == "Custom Name"
        assert result.schedule == "*/5 * * * *"
        assert result.prompt == "Monitor system"
        assert result.context_mode == "isolated"
        assert result.is_periodic is True

    def test_loads_workspace_only_config(self, tmp_path, monkeypatch):
        """Config with only workspace fields (no schedule) should load with is_periodic=False."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(
            tmp_path,
            "test-group",
            {
                "is_god": True,
                "requires_trigger": False,
            },
        )

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.is_god is True
        assert result.requires_trigger is False
        assert result.is_periodic is False
        assert result.schedule is None
        assert result.prompt is None

    def test_returns_none_when_file_not_exists(self, tmp_path, monkeypatch):
        """Should return None when workspace.yaml doesn't exist."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        result = load_workspace_config("nonexistent-group")
        assert result is None

    def test_returns_none_when_yaml_is_not_dict(self, tmp_path, monkeypatch):
        """Should return None when YAML root is not a dictionary."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        config_path = tmp_path / "test-group" / "workspace.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(yaml.dump(["schedule", "prompt"]))

        result = load_workspace_config("test-group")
        assert result is None

    def test_empty_file_returns_defaults(self, tmp_path, monkeypatch):
        """Empty workspace.yaml should return config with all defaults."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", None)

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.is_god is False
        assert result.requires_trigger is True
        assert result.project_access is False
        assert result.name is None
        assert result.is_periodic is False

    def test_schedule_without_prompt_is_not_periodic(self, tmp_path, monkeypatch):
        """Schedule without prompt should load but is_periodic=False."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"schedule": "0 9 * * *"})

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.schedule == "0 9 * * *"
        assert result.prompt is None
        assert result.is_periodic is False

    def test_prompt_without_schedule_is_not_periodic(self, tmp_path, monkeypatch):
        """Prompt without schedule should load but is_periodic=False."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"prompt": "Do something"})

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.prompt == "Do something"
        assert result.schedule is None
        assert result.is_periodic is False

    def test_invalid_cron_nullifies_schedule(self, tmp_path, monkeypatch):
        """Invalid cron expression should result in schedule=None."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(
            tmp_path,
            "test-group",
            {
                "schedule": "not a valid cron",
                "prompt": "Check for updates",
            },
        )

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.schedule is None
        assert result.is_periodic is False

    def test_defaults_to_group_context_mode_when_invalid(self, tmp_path, monkeypatch):
        """Should default to 'group' when context_mode is invalid."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(
            tmp_path,
            "test-group",
            {
                "schedule": "0 9 * * *",
                "prompt": "Check for updates",
                "context_mode": "invalid_mode",
            },
        )

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.context_mode == "group"

    def test_handles_various_valid_cron_expressions(self, tmp_path, monkeypatch):
        """Should accept various valid cron expression formats."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        valid_crons = [
            "0 9 * * *",
            "*/5 * * * *",
            "0 0 * * 0",
            "0 0 1 * *",
            "0 */2 * * *",
        ]

        for cron in valid_crons:
            _write_yaml(
                tmp_path,
                "test-group",
                {
                    "schedule": cron,
                    "prompt": "Test",
                },
            )
            result = load_workspace_config("test-group")
            assert result is not None, f"Failed for cron: {cron}"
            assert result.schedule == cron

    def test_converts_schedule_to_string(self, tmp_path, monkeypatch):
        """Should convert non-string schedule values to strings."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        config_path = tmp_path / "test-group" / "workspace.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("schedule: 12345\nprompt: Test\n")

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.schedule is None  # "12345" is not a valid cron

    def test_converts_prompt_to_string(self, tmp_path, monkeypatch):
        """Should convert non-string prompt values to strings."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        config_path = tmp_path / "test-group" / "workspace.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("schedule: '0 9 * * *'\nprompt: 12345\n")

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.prompt == "12345"

    def test_project_access_converts_to_bool(self, tmp_path, monkeypatch):
        """Should convert project_access to boolean."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        for value in [True, "true", 1, "yes"]:
            _write_yaml(tmp_path, "test-group", {"project_access": value})
            result = load_workspace_config("test-group")
            assert result is not None
            assert result.project_access is True

        for value in [False, "", 0]:
            _write_yaml(tmp_path, "test-group", {"project_access": value})
            result = load_workspace_config("test-group")
            assert result is not None
            assert result.project_access is False

    def test_is_god_defaults_to_false(self, tmp_path, monkeypatch):
        """is_god should default to False when omitted."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"requires_trigger": False})

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.is_god is False

    def test_requires_trigger_defaults_to_true(self, tmp_path, monkeypatch):
        """requires_trigger should default to True when omitted."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"is_god": True})

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.requires_trigger is True

    def test_name_field_preserved(self, tmp_path, monkeypatch):
        """Custom name should be preserved when set."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"name": "My Custom Agent"})

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.name == "My Custom Agent"

    def test_name_none_when_omitted(self, tmp_path, monkeypatch):
        """Name should be None when not specified (caller uses folder name)."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"is_god": True})

        result = load_workspace_config("test-group")
        assert result is not None
        assert result.name is None


class TestWriteWorkspaceConfig:
    """Test the write_workspace_config() function."""

    def test_writes_workspace_only_config(self, tmp_path, monkeypatch):
        """Should write workspace-only config (no schedule fields)."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        config = WorkspaceConfig(is_god=True, requires_trigger=False)
        path = write_workspace_config("test-group", config)

        assert path.exists()
        content = yaml.safe_load(path.read_text())
        assert content["is_god"] is True
        assert content["requires_trigger"] is False
        assert "schedule" not in content
        assert "prompt" not in content

    def test_writes_periodic_config(self, tmp_path, monkeypatch):
        """Should write config with scheduling fields."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        config = WorkspaceConfig(
            schedule="*/5 * * * *",
            prompt="Monitor system",
            context_mode="isolated",
            project_access=True,
        )
        path = write_workspace_config("test-group", config)

        content = yaml.safe_load(path.read_text())
        assert content["schedule"] == "*/5 * * * *"
        assert content["prompt"] == "Monitor system"
        assert content["context_mode"] == "isolated"
        assert content["project_access"] is True

    def test_omits_default_values(self, tmp_path, monkeypatch):
        """Should omit fields that match defaults."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        config = WorkspaceConfig()  # all defaults
        path = write_workspace_config("test-group", config)

        raw = path.read_text()
        # Empty or minimal — no is_god, requires_trigger, etc.
        content = yaml.safe_load(raw)
        assert content is None  # empty YAML

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        """Should create group directory if it doesn't exist."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        config = WorkspaceConfig(is_god=True)
        path = write_workspace_config("new-group", config)

        assert path.exists()
        assert path.parent.name == "new-group"

    def test_overwrites_existing_config(self, tmp_path, monkeypatch):
        """Should overwrite existing workspace.yaml file."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        config_path = tmp_path / "test-group" / "workspace.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("old content")

        config = WorkspaceConfig(is_god=True)
        path = write_workspace_config("test-group", config)

        assert "old content" not in path.read_text()
        content = yaml.safe_load(path.read_text())
        assert content["is_god"] is True

    def test_roundtrip_workspace_only(self, tmp_path, monkeypatch):
        """Workspace-only config should survive write->read cycle."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        original = WorkspaceConfig(
            is_god=True,
            requires_trigger=False,
            name="My Agent",
        )
        write_workspace_config("test-group", original)
        loaded = load_workspace_config("test-group")

        assert loaded is not None
        assert loaded.is_god == original.is_god
        assert loaded.requires_trigger == original.requires_trigger
        assert loaded.name == original.name
        assert loaded.is_periodic is False

    def test_roundtrip_periodic(self, tmp_path, monkeypatch):
        """Periodic config should survive write->read cycle."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        original = WorkspaceConfig(
            project_access=True,
            schedule="*/15 * * * *",
            prompt="Run periodic checks",
            context_mode="isolated",
        )
        write_workspace_config("test-group", original)
        loaded = load_workspace_config("test-group")

        assert loaded is not None
        assert loaded.schedule == original.schedule
        assert loaded.prompt == original.prompt
        assert loaded.context_mode == original.context_mode
        assert loaded.project_access == original.project_access
        assert loaded.is_periodic is True

    def test_includes_name_when_set(self, tmp_path, monkeypatch):
        """Should include name in YAML when set."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        config = WorkspaceConfig(name="Custom Name")
        path = write_workspace_config("test-group", config)

        content = yaml.safe_load(path.read_text())
        assert content["name"] == "Custom Name"


class TestWorkspaceConfig:
    """Test the WorkspaceConfig dataclass."""

    def test_defaults(self):
        """Should create config with sensible defaults."""
        config = WorkspaceConfig()
        assert config.is_god is False
        assert config.requires_trigger is True
        assert config.project_access is False
        assert config.name is None
        assert config.schedule is None
        assert config.prompt is None
        assert config.context_mode == "group"
        assert config.is_periodic is False

    def test_is_periodic_true(self):
        """is_periodic should be True when both schedule and prompt are set."""
        config = WorkspaceConfig(schedule="0 9 * * *", prompt="Test")
        assert config.is_periodic is True

    def test_is_periodic_false_no_schedule(self):
        """is_periodic should be False when schedule is missing."""
        config = WorkspaceConfig(prompt="Test")
        assert config.is_periodic is False

    def test_is_periodic_false_no_prompt(self):
        """is_periodic should be False when prompt is missing."""
        config = WorkspaceConfig(schedule="0 9 * * *")
        assert config.is_periodic is False


class TestHasProjectAccess:
    """Test the has_project_access() helper."""

    def test_god_group_always_has_access(self, tmp_path, monkeypatch):
        """God groups should have project_access regardless of config."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        @dataclass
        class FakeGroup:
            is_god: bool = True
            folder: str = "god-group"

        assert has_project_access(FakeGroup()) is True

    def test_project_access_from_config(self, tmp_path, monkeypatch):
        """Non-god group with project_access=True in config should return True."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"project_access": True})

        @dataclass
        class FakeGroup:
            is_god: bool = False
            folder: str = "test-group"

        assert has_project_access(FakeGroup()) is True

    def test_no_config_returns_false(self, tmp_path, monkeypatch):
        """Group with no workspace.yaml should return False."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        @dataclass
        class FakeGroup:
            is_god: bool = False
            folder: str = "no-config"

        assert has_project_access(FakeGroup()) is False

    def test_config_without_project_access(self, tmp_path, monkeypatch):
        """Group with config but no project_access should return False."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "test-group", {"is_god": False})

        @dataclass
        class FakeGroup:
            is_god: bool = False
            folder: str = "test-group"

        assert has_project_access(FakeGroup()) is False


class TestGetProjectAccessFolders:
    """Test the get_project_access_folders() helper."""

    def test_returns_god_folders(self, tmp_path, monkeypatch):
        """Should include god group folders."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)

        @dataclass
        class FakeProfile:
            is_god: bool
            folder: str

        workspaces = {
            "jid1": FakeProfile(is_god=True, folder="admin"),
            "jid2": FakeProfile(is_god=False, folder="regular"),
        }

        result = get_project_access_folders(workspaces)
        assert "admin" in result
        assert "regular" not in result

    def test_returns_project_access_folders(self, tmp_path, monkeypatch):
        """Should include folders with project_access in config."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "code-improver", {"project_access": True})

        @dataclass
        class FakeProfile:
            is_god: bool
            folder: str

        workspaces = {
            "jid1": FakeProfile(is_god=False, folder="code-improver"),
            "jid2": FakeProfile(is_god=False, folder="no-config"),
        }

        result = get_project_access_folders(workspaces)
        assert "code-improver" in result
        assert "no-config" not in result

    def test_combined(self, tmp_path, monkeypatch):
        """Should return both god and project_access folders."""
        monkeypatch.setattr("pynchy.workspace_config.GROUPS_DIR", tmp_path)
        _write_yaml(tmp_path, "code-improver", {"project_access": True})

        @dataclass
        class FakeProfile:
            is_god: bool
            folder: str

        workspaces = {
            "jid1": FakeProfile(is_god=True, folder="admin"),
            "jid2": FakeProfile(is_god=False, folder="code-improver"),
            "jid3": FakeProfile(is_god=False, folder="plain"),
        }

        result = get_project_access_folders(workspaces)
        assert set(result) == {"admin", "code-improver"}
