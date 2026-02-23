"""Tests for workspace configuration helpers backed by Settings."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from conftest import make_settings

from pynchy.config import WorkspaceConfig, WorkspaceDefaultsConfig
from pynchy.workspace_config import (
    configure_plugin_workspaces,
    get_repo_access,
    get_repo_access_groups,
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
            workspaces={"team": WorkspaceConfig(name="test", is_admin=False)},
            defaults=WorkspaceDefaultsConfig(trigger="always", context_mode="isolated"),
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("team")

        assert cfg is not None
        assert cfg.trigger is None  # trigger cascaded in resolve_channel_config, not here
        assert cfg.context_mode == "isolated"
        assert cfg.is_periodic is False

    def test_keeps_explicit_workspace_fields(self):
        s = _settings_with_workspaces(
            workspaces={
                "daily": WorkspaceConfig(
                    is_admin=True,
                    trigger="mention",
                    repo_access="owner/pynchy",
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
        assert cfg.repo_access == "owner/pynchy"
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
                            "name": "test",
                            "repo_access": "owner/repo",
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
        assert cfg.repo_access == "owner/repo"
        assert cfg.is_periodic is True


class TestWorkspaceConfigModel:
    def test_defaults(self):
        cfg = WorkspaceConfig(name="test")
        assert cfg.is_admin is False
        assert cfg.trigger is None
        assert cfg.repo_access is None
        assert cfg.context_mode is None
        assert cfg.is_periodic is False

    def test_is_periodic(self):
        assert WorkspaceConfig(name="test", schedule="0 9 * * *", prompt="x").is_periodic is True
        assert WorkspaceConfig(name="test", schedule="0 9 * * *").is_periodic is False


class TestGetRepoAccess:
    def test_returns_none_when_no_config(self):
        @dataclass
        class FakeGroup:
            is_admin: bool = False
            folder: str = "dev"

        s = _settings_with_workspaces(workspaces={})
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            assert get_repo_access(FakeGroup()) is None

    def test_returns_slug_from_config(self):
        @dataclass
        class FakeGroup:
            is_admin: bool = False
            folder: str = "dev"

        s = _settings_with_workspaces(
            workspaces={"dev": WorkspaceConfig(name="test", repo_access="owner/myrepo")}
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            assert get_repo_access(FakeGroup()) == "owner/myrepo"

    def test_admin_without_explicit_repo_access_returns_none(self):
        """Admin groups no longer get implicit repo access."""

        @dataclass
        class FakeGroup:
            is_admin: bool = True
            folder: str = "admin-1"

        s = _settings_with_workspaces(
            workspaces={"admin-1": WorkspaceConfig(name="test", is_admin=True)}
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            assert get_repo_access(FakeGroup()) is None


class TestGetRepoAccessGroups:
    def test_maps_slug_to_folders(self):
        @dataclass
        class FakeProfile:
            is_admin: bool
            folder: str

        workspaces = {
            "jid1": FakeProfile(is_admin=False, folder="code-improver"),
            "jid2": FakeProfile(is_admin=False, folder="plain"),
            "jid3": FakeProfile(is_admin=False, folder="other-project"),
        }
        s = _settings_with_workspaces(
            workspaces={
                "code-improver": WorkspaceConfig(name="test", repo_access="owner/pynchy"),
                "plain": WorkspaceConfig(name="test", repo_access=None),
                "other-project": WorkspaceConfig(name="test", repo_access="owner/pynchy"),
            }
        )
        with patch("pynchy.workspace_config.get_settings", return_value=s):
            result = get_repo_access_groups(workspaces)

        assert "owner/pynchy" in result
        assert set(result["owner/pynchy"]) == {"code-improver", "other-project"}
        assert "plain" not in str(result)
