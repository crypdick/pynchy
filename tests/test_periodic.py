"""Tests for periodic agent configuration.

Tests the YAML loading, validation, and writing logic for periodic agents.
This is critical business logic - incorrect parsing could cause agents to run
with wrong schedules or fail to load valid configurations.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from pynchy.config import GROUPS_DIR
from pynchy.periodic import PeriodicAgentConfig, load_periodic_config, write_periodic_config


class TestLoadPeriodicConfig:
    """Test the load_periodic_config() function which parses periodic.yaml files."""

    def test_loads_valid_minimal_config(self, tmp_path, monkeypatch):
        """Valid config with only required fields should load successfully."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text(
            yaml.dump(
                {
                    "schedule": "0 9 * * *",
                    "prompt": "Check for updates",
                }
            )
        )

        result = load_periodic_config(group_folder)
        assert result is not None
        assert result.schedule == "0 9 * * *"
        assert result.prompt == "Check for updates"
        assert result.context_mode == "group"  # default
        assert result.project_access is False  # default

    def test_loads_config_with_all_fields(self, tmp_path, monkeypatch):
        """Config with all optional fields should load correctly."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text(
            yaml.dump(
                {
                    "schedule": "*/5 * * * *",
                    "prompt": "Monitor system",
                    "context_mode": "isolated",
                    "project_access": True,
                }
            )
        )

        result = load_periodic_config(group_folder)
        assert result is not None
        assert result.schedule == "*/5 * * * *"
        assert result.prompt == "Monitor system"
        assert result.context_mode == "isolated"
        assert result.project_access is True

    def test_returns_none_when_file_not_exists(self, tmp_path, monkeypatch):
        """Should return None when periodic.yaml doesn't exist."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        result = load_periodic_config("nonexistent-group")
        assert result is None

    def test_returns_none_when_yaml_is_not_dict(self, tmp_path, monkeypatch):
        """Should return None when YAML root is not a dictionary."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        # Write a list instead of a dict
        config_path.write_text(yaml.dump(["schedule", "prompt"]))

        result = load_periodic_config(group_folder)
        assert result is None

    def test_returns_none_when_schedule_missing(self, tmp_path, monkeypatch):
        """Should return None when schedule field is missing."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text(
            yaml.dump(
                {
                    "prompt": "Check for updates",
                }
            )
        )

        result = load_periodic_config(group_folder)
        assert result is None

    def test_returns_none_when_prompt_missing(self, tmp_path, monkeypatch):
        """Should return None when prompt field is missing."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text(
            yaml.dump(
                {
                    "schedule": "0 9 * * *",
                }
            )
        )

        result = load_periodic_config(group_folder)
        assert result is None

    def test_returns_none_when_cron_expression_invalid(self, tmp_path, monkeypatch):
        """Should return None when cron expression is invalid."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text(
            yaml.dump(
                {
                    "schedule": "not a valid cron",
                    "prompt": "Check for updates",
                }
            )
        )

        result = load_periodic_config(group_folder)
        assert result is None

    def test_defaults_to_group_context_mode_when_invalid(self, tmp_path, monkeypatch):
        """Should default to 'group' when context_mode is invalid."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text(
            yaml.dump(
                {
                    "schedule": "0 9 * * *",
                    "prompt": "Check for updates",
                    "context_mode": "invalid_mode",
                }
            )
        )

        result = load_periodic_config(group_folder)
        assert result is not None
        assert result.context_mode == "group"

    def test_handles_various_valid_cron_expressions(self, tmp_path, monkeypatch):
        """Should accept various valid cron expression formats."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        valid_crons = [
            "0 9 * * *",  # Daily at 9am
            "*/5 * * * *",  # Every 5 minutes
            "0 0 * * 0",  # Weekly on Sunday
            "0 0 1 * *",  # Monthly on the 1st
            "0 */2 * * *",  # Every 2 hours
        ]

        for cron in valid_crons:
            config_path.write_text(
                yaml.dump(
                    {
                        "schedule": cron,
                        "prompt": "Test",
                    }
                )
            )
            result = load_periodic_config(group_folder)
            assert result is not None, f"Failed for cron: {cron}"
            assert result.schedule == cron

    def test_converts_schedule_to_string(self, tmp_path, monkeypatch):
        """Should convert non-string schedule values to strings."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        # Write schedule as an integer (YAML might parse it that way)
        config_path.write_text("schedule: 12345\nprompt: Test\n")

        result = load_periodic_config(group_folder)
        # Should fail validation because "12345" is not a valid cron
        assert result is None

    def test_converts_prompt_to_string(self, tmp_path, monkeypatch):
        """Should convert non-string prompt values to strings."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text("schedule: '0 9 * * *'\nprompt: 12345\n")

        result = load_periodic_config(group_folder)
        assert result is not None
        assert result.prompt == "12345"

    def test_project_access_defaults_to_false(self, tmp_path, monkeypatch):
        """Should default project_access to False when omitted."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        config_path.write_text(
            yaml.dump(
                {
                    "schedule": "0 9 * * *",
                    "prompt": "Test",
                }
            )
        )

        result = load_periodic_config(group_folder)
        assert result is not None
        assert result.project_access is False

    def test_project_access_converts_to_bool(self, tmp_path, monkeypatch):
        """Should convert project_access to boolean."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        # Test truthy values
        for value in [True, "true", 1, "yes"]:
            config_path.write_text(
                yaml.dump(
                    {
                        "schedule": "0 9 * * *",
                        "prompt": "Test",
                        "project_access": value,
                    }
                )
            )
            result = load_periodic_config(group_folder)
            assert result is not None
            assert result.project_access is True

        # Test falsy values
        for value in [False, "", 0, None]:
            config_path.write_text(
                yaml.dump(
                    {
                        "schedule": "0 9 * * *",
                        "prompt": "Test",
                        "project_access": value,
                    }
                )
            )
            result = load_periodic_config(group_folder)
            assert result is not None
            assert result.project_access is False


class TestWritePeriodicConfig:
    """Test the write_periodic_config() function which writes periodic.yaml files."""

    def test_writes_minimal_config(self, tmp_path, monkeypatch):
        """Should write config with only required fields."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"

        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="Check for updates",
        )

        path = write_periodic_config(group_folder, config)

        assert path.exists()
        assert path == tmp_path / group_folder / "periodic.yaml"

        # Verify content
        content = yaml.safe_load(path.read_text())
        assert content["schedule"] == "0 9 * * *"
        assert content["prompt"] == "Check for updates"
        assert content["context_mode"] == "group"
        assert "project_access" not in content  # Should be omitted when False

    def test_writes_config_with_all_fields(self, tmp_path, monkeypatch):
        """Should write config with all fields including optional ones."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"

        config = PeriodicAgentConfig(
            schedule="*/5 * * * *",
            prompt="Monitor system",
            context_mode="isolated",
            project_access=True,
        )

        path = write_periodic_config(group_folder, config)

        assert path.exists()

        # Verify content
        content = yaml.safe_load(path.read_text())
        assert content["schedule"] == "*/5 * * * *"
        assert content["prompt"] == "Monitor system"
        assert content["context_mode"] == "isolated"
        assert content["project_access"] is True

    def test_creates_parent_directory_if_not_exists(self, tmp_path, monkeypatch):
        """Should create group directory if it doesn't exist."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "new-group"

        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="Test",
        )

        path = write_periodic_config(group_folder, config)

        assert path.exists()
        assert path.parent.exists()
        assert path.parent.name == group_folder

    def test_omits_project_access_when_false(self, tmp_path, monkeypatch):
        """Should omit project_access from YAML when False."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"

        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="Test",
            project_access=False,
        )

        path = write_periodic_config(group_folder, config)

        content = yaml.safe_load(path.read_text())
        assert "project_access" not in content

    def test_includes_project_access_when_true(self, tmp_path, monkeypatch):
        """Should include project_access in YAML when True."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"

        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="Test",
            project_access=True,
        )

        path = write_periodic_config(group_folder, config)

        content = yaml.safe_load(path.read_text())
        assert content["project_access"] is True

    def test_overwrites_existing_config(self, tmp_path, monkeypatch):
        """Should overwrite existing periodic.yaml file."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"
        config_path = tmp_path / group_folder / "periodic.yaml"
        config_path.parent.mkdir(parents=True)

        # Write initial config
        config_path.write_text("old content")

        # Write new config
        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="New config",
        )

        path = write_periodic_config(group_folder, config)

        content = yaml.safe_load(path.read_text())
        assert content["prompt"] == "New config"
        assert "old content" not in path.read_text()

    def test_roundtrip_preservation(self, tmp_path, monkeypatch):
        """Should preserve all fields through write->read cycle."""
        monkeypatch.setattr("pynchy.periodic.GROUPS_DIR", tmp_path)
        group_folder = "test-group"

        original = PeriodicAgentConfig(
            schedule="*/15 * * * *",
            prompt="Run periodic checks",
            context_mode="isolated",
            project_access=True,
        )

        write_periodic_config(group_folder, original)
        loaded = load_periodic_config(group_folder)

        assert loaded is not None
        assert loaded.schedule == original.schedule
        assert loaded.prompt == original.prompt
        assert loaded.context_mode == original.context_mode
        assert loaded.project_access == original.project_access


class TestPeriodicAgentConfig:
    """Test the PeriodicAgentConfig dataclass."""

    def test_creates_with_required_fields(self):
        """Should create config with only required fields."""
        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="Test",
        )

        assert config.schedule == "0 9 * * *"
        assert config.prompt == "Test"
        assert config.context_mode == "group"
        assert config.project_access is False

    def test_creates_with_all_fields(self):
        """Should create config with all fields."""
        config = PeriodicAgentConfig(
            schedule="*/5 * * * *",
            prompt="Monitor",
            context_mode="isolated",
            project_access=True,
        )

        assert config.schedule == "*/5 * * * *"
        assert config.prompt == "Monitor"
        assert config.context_mode == "isolated"
        assert config.project_access is True

    def test_context_mode_accepts_group(self):
        """Should accept 'group' as context_mode."""
        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="Test",
            context_mode="group",
        )
        assert config.context_mode == "group"

    def test_context_mode_accepts_isolated(self):
        """Should accept 'isolated' as context_mode."""
        config = PeriodicAgentConfig(
            schedule="0 9 * * *",
            prompt="Test",
            context_mode="isolated",
        )
        assert config.context_mode == "isolated"
