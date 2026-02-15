"""Tests for workspace configuration helpers backed by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

from pynchy.config import (
    AgentConfig,
    CommandWordsConfig,
    ContainerConfig,
    IntervalsConfig,
    LoggingConfig,
    QueueConfig,
    SchedulerConfig,
    SecretsConfig,
    SecurityConfig,
    ServerConfig,
    Settings,
    WorkspaceConfig,
    WorkspaceDefaultsConfig,
)
from pynchy.workspace_config import (
    get_project_access_folders,
    has_project_access,
    load_workspace_config,
    write_workspace_config,
)


def _settings_with_workspaces(
    *,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    defaults: WorkspaceDefaultsConfig | None = None,
):
    return Settings.model_construct(
        agent=AgentConfig(),
        container=ContainerConfig(),
        server=ServerConfig(),
        logging=LoggingConfig(),
        secrets=SecretsConfig(),
        workspace_defaults=defaults or WorkspaceDefaultsConfig(),
        workspaces=workspaces or {},
        commands=CommandWordsConfig(),
        scheduler=SchedulerConfig(),
        intervals=IntervalsConfig(),
        queue=QueueConfig(),
        security=SecurityConfig(),
    )


class TestLoadWorkspaceConfig:
    def test_returns_none_when_missing(self):
        s = _settings_with_workspaces(workspaces={})
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            assert load_workspace_config("missing") is None

    def test_applies_workspace_defaults(self):
        s = _settings_with_workspaces(
            workspaces={"team": WorkspaceConfig(is_god=False)},
            defaults=WorkspaceDefaultsConfig(requires_trigger=False, context_mode="isolated"),
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("team")

        assert cfg is not None
        assert cfg.requires_trigger is False
        assert cfg.context_mode == "isolated"
        assert cfg.is_periodic is False

    def test_keeps_explicit_workspace_fields(self):
        s = _settings_with_workspaces(
            workspaces={
                "daily": WorkspaceConfig(
                    is_god=True,
                    requires_trigger=True,
                    project_access=True,
                    name="Daily Agent",
                    schedule="0 9 * * *",
                    prompt="Run checks",
                    context_mode="group",
                )
            }
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("daily")

        assert cfg is not None
        assert cfg.is_god is True
        assert cfg.project_access is True
        assert cfg.name == "Daily Agent"
        assert cfg.is_periodic is True


class TestWriteWorkspaceConfig:
    def test_writes_via_add_workspace_to_toml(self):
        cfg = WorkspaceConfig(project_access=True, schedule="*/5 * * * *", prompt="Monitor")
        with patch("pynchy.workspace_config.add_workspace_to_toml") as add_ws:
            write_workspace_config("ops", cfg)
        add_ws.assert_called_once_with("ops", cfg)


class TestWorkspaceConfigModel:
    def test_defaults(self):
        cfg = WorkspaceConfig()
        assert cfg.is_god is False
        assert cfg.requires_trigger is None
        assert cfg.project_access is False
        assert cfg.context_mode is None
        assert cfg.is_periodic is False

    def test_is_periodic(self):
        assert WorkspaceConfig(schedule="0 9 * * *", prompt="x").is_periodic is True
        assert WorkspaceConfig(schedule="0 9 * * *").is_periodic is False


class TestHasProjectAccess:
    def test_god_always_true(self):
        @dataclass
        class FakeGroup:
            is_god: bool = True
            folder: str = "god"

        assert has_project_access(FakeGroup()) is True

    def test_non_god_uses_workspace_setting(self):
        @dataclass
        class FakeGroup:
            is_god: bool = False
            folder: str = "dev"

        s = _settings_with_workspaces(workspaces={"dev": WorkspaceConfig(project_access=True)})
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            assert has_project_access(FakeGroup()) is True


class TestGetProjectAccessFolders:
    def test_includes_god_and_project_access(self):
        @dataclass
        class FakeProfile:
            is_god: bool
            folder: str

        workspaces = {
            "jid1": FakeProfile(is_god=True, folder="admin"),
            "jid2": FakeProfile(is_god=False, folder="code-improver"),
            "jid3": FakeProfile(is_god=False, folder="plain"),
        }
        s = _settings_with_workspaces(
            workspaces={
                "code-improver": WorkspaceConfig(project_access=True),
                "plain": WorkspaceConfig(project_access=False),
            }
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            result = get_project_access_folders(workspaces)

        assert set(result) == {"admin", "code-improver"}
