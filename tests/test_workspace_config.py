"""Tests for workspace configuration helpers backed by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from conftest import make_settings

from pynchy.config import WorkspaceConfig, WorkspaceDefaultsConfig
from pynchy.workspace_config import (
    configure_plugin_workspaces,
    get_pynchy_repo_access_folders,
    has_pynchy_repo_access,
    load_workspace_config,
)


def _settings_with_workspaces(
    *,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    defaults: WorkspaceDefaultsConfig | None = None,
):
    return make_settings(
        workspaces=workspaces or {},
        workspace_defaults=defaults or WorkspaceDefaultsConfig(),
    )


class TestLoadWorkspaceConfig:
    def teardown_method(self):
        configure_plugin_workspaces(None)

    def test_returns_none_when_missing(self):
        s = _settings_with_workspaces(workspaces={})
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            assert load_workspace_config("missing") is None

    def test_applies_workspace_defaults(self):
        s = _settings_with_workspaces(
            workspaces={"team": WorkspaceConfig(is_admin=False)},
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
                    is_admin=True,
                    requires_trigger=True,
                    pynchy_repo_access=True,
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
        assert cfg.is_admin is True
        assert cfg.pynchy_repo_access is True
        assert cfg.name == "Daily Agent"
        assert cfg.is_periodic is True

    def test_loads_workspace_from_plugin_spec(self):
        s = _settings_with_workspaces(workspaces={})
        fake_pm = SimpleNamespace(
            hook=SimpleNamespace(
                pynchy_workspace_spec=lambda: [
                    {
                        "folder": "code-improver",
                        "config": {
                            "pynchy_repo_access": True,
                            "schedule": "0 4 * * *",
                            "prompt": "Improve code",
                            "context_mode": "isolated",
                        },
                    }
                ]
            )
        )
        configure_plugin_workspaces(fake_pm)

        with patch("pynchy.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("code-improver")

        assert cfg is not None
        assert cfg.pynchy_repo_access is True
        assert cfg.is_periodic is True


class TestWorkspaceConfigModel:
    def test_defaults(self):
        cfg = WorkspaceConfig()
        assert cfg.is_admin is False
        assert cfg.requires_trigger is None
        assert cfg.pynchy_repo_access is False
        assert cfg.context_mode is None
        assert cfg.is_periodic is False

    def test_is_periodic(self):
        assert WorkspaceConfig(schedule="0 9 * * *", prompt="x").is_periodic is True
        assert WorkspaceConfig(schedule="0 9 * * *").is_periodic is False


class TestHasProjectAccess:
    def test_admin_always_true(self):
        @dataclass
        class FakeGroup:
            is_admin: bool = True
            folder: str = "god"

        assert has_pynchy_repo_access(FakeGroup()) is True

    def test_non_admin_uses_workspace_setting(self):
        @dataclass
        class FakeGroup:
            is_admin: bool = False
            folder: str = "dev"

        s = _settings_with_workspaces(workspaces={"dev": WorkspaceConfig(pynchy_repo_access=True)})
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            assert has_pynchy_repo_access(FakeGroup()) is True


class TestGetProjectAccessFolders:
    def test_includes_admin_and_pynchy_repo_access(self):
        @dataclass
        class FakeProfile:
            is_admin: bool
            folder: str

        workspaces = {
            "jid1": FakeProfile(is_admin=True, folder="admin"),
            "jid2": FakeProfile(is_admin=False, folder="code-improver"),
            "jid3": FakeProfile(is_admin=False, folder="plain"),
        }
        s = _settings_with_workspaces(
            workspaces={
                "code-improver": WorkspaceConfig(pynchy_repo_access=True),
                "plain": WorkspaceConfig(pynchy_repo_access=False),
            }
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            result = get_pynchy_repo_access_folders(workspaces)

        assert set(result) == {"admin", "code-improver"}
