"""Tests for workspace configuration helpers backed by composable Settings."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pluggy
import pytest
from conftest import make_settings
from pydantic import ValidationError

from pynchy.config import WorkspaceConfig
from pynchy.config.models import ProfileConfig
from pynchy.host.orchestrator.workspace_config import (
    add_workspace_to_toml,
    configure_plugin_workspaces,
    get_repo_access,
    get_repo_access_groups,
    load_resolved_config,
    load_workspace_config,
    reconcile_workspaces,
)
from pynchy.types import InboundFetchResult, OutboundEvent, WorkspaceProfile, WorkspaceSecurity


class _FakePM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, hook: SimpleNamespace) -> None:
        self.hook = hook


class _FakeChannel:
    name = "connections.synapse"
    formatter = object()

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return False

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        return InboundFetchResult(messages=[])


def _settings_with_workspaces(
    *,
    profiles: dict[str, ProfileConfig] | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
):
    return make_settings(
        profiles=profiles or {},
        workspaces=workspaces or {},
    )


class TestLoadWorkspaceConfig:
    def teardown_method(self):
        configure_plugin_workspaces(None)

    def test_returns_none_when_missing(self):
        s = _settings_with_workspaces(workspaces={})
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert load_workspace_config("missing") is None

    def test_keeps_selected_profiles_only(self):
        s = _settings_with_workspaces(
            profiles={"base": ProfileConfig(), "admin": ProfileConfig(is_admin=True)},
            workspaces={"team": WorkspaceConfig(profiles=["base", "admin"])},
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("team")

        assert cfg is not None
        assert cfg.profiles == ["base", "admin"]

    def test_loads_workspace_from_plugin_spec(self):
        s = _settings_with_workspaces(workspaces={})
        fake_pm = _FakePM(
            SimpleNamespace(
                pynchy_workspace_spec=lambda: [
                    {
                        "folder": "code-improver",
                        "config": {"profiles": ["worker"]},
                    }
                ]
            )
        )
        configure_plugin_workspaces(fake_pm)

        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("code-improver")

        assert cfg is not None
        assert cfg.profiles == ["worker"]


class TestLoadResolvedConfig:
    def test_resolves_selected_profiles(self):
        s = _settings_with_workspaces(
            profiles={
                "base": ProfileConfig(repo=["owner/base"], prompts=["base"]),
                "dev": ProfileConfig(includes=["base"], repo=["owner/dev"], is_admin=True),
            },
            workspaces={"dev": WorkspaceConfig(profiles=["dev"])},
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            resolved = load_resolved_config("dev")

        assert resolved is not None
        assert resolved.prompts == ["base"]
        assert resolved.repo == ["owner/base", "owner/dev"]
        assert resolved.is_admin is True


class TestWorkspaceConfigModel:
    def test_defaults(self):
        cfg = WorkspaceConfig()
        assert cfg.profiles == []

    def test_rejects_deleted_workspace_fields(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            WorkspaceConfig(profile="old")


class TestAddWorkspaceToToml:
    def test_rejects_workspace_that_does_not_round_trip_through_settings(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            """
[profiles.worker]
"""
        )

        with pytest.raises(ValueError, match="unknown profile"):
            add_workspace_to_toml("daily", WorkspaceConfig(profiles=["missing"]))

        assert "workspaces.daily" not in toml_path.read_text()

    def test_writes_workspace_profiles(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            """
[profiles.pynchy-dev]
is_admin = true
"""
        )

        add_workspace_to_toml("discord-admin", WorkspaceConfig(profiles=["pynchy-dev"]))

        data = tomllib.loads(toml_path.read_text())
        assert data["workspaces"]["discord-admin"]["profiles"] == ["pynchy-dev"]


class TestGetRepoAccess:
    def test_returns_none_when_no_config(self):
        s = _settings_with_workspaces(workspaces={})
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert get_repo_access("dev") is None

    def test_returns_first_resolved_repo_slug(self):
        s = _settings_with_workspaces(
            profiles={
                "base": ProfileConfig(repo=["owner/base"]),
                "dev": ProfileConfig(includes=["base"], repo=["owner/dev"]),
            },
            workspaces={"dev": WorkspaceConfig(profiles=["dev"])},
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert get_repo_access("dev") == "owner/base"

    def test_admin_without_repo_returns_none(self):
        s = _settings_with_workspaces(
            profiles={"admin": ProfileConfig(is_admin=True)},
            workspaces={"admin-1": WorkspaceConfig(profiles=["admin"])},
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert get_repo_access("admin-1") is None


class TestGetRepoAccessGroups:
    def test_maps_each_resolved_slug_to_folders(self):
        s = _settings_with_workspaces(
            profiles={
                "shared": ProfileConfig(repo=["owner/shared"]),
                "code": ProfileConfig(includes=["shared"], repo=["owner/pynchy"]),
                "other": ProfileConfig(repo=["owner/pynchy"]),
                "plain": ProfileConfig(),
            },
            workspaces={
                "code-improver": WorkspaceConfig(profiles=["code"]),
                "plain": WorkspaceConfig(profiles=["plain"]),
                "other-project": WorkspaceConfig(profiles=["other"]),
            },
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            result = get_repo_access_groups(["code-improver", "plain", "other-project"])

        assert result["owner/shared"] == ["code-improver"]
        assert set(result["owner/pynchy"]) == {"code-improver", "other-project"}
        assert "plain" not in str(result)


@pytest.mark.asyncio
async def test_reconcile_syncs_existing_profile_admin_and_contains_secrets():
    s = make_settings(
        profiles={"admin": ProfileConfig(is_admin=True, contains_secrets=True)},
        workspaces={"admin": WorkspaceConfig(profiles=["admin"])},
    )
    existing = WorkspaceProfile(
        jid="slack:CADMIN",
        name="Admin",
        folder="admin",
        trigger="@pynchy",
        is_admin=False,
        security=WorkspaceSecurity(contains_secrets=False),
    )
    workspaces = {existing.jid: existing}
    register = AsyncMock()

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s),
        patch(
            "pynchy.host.orchestrator.workspace_config.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_registration.set_workspace_profile",
            new_callable=AsyncMock,
        ),
    ):
        await reconcile_workspaces(workspaces, [_FakeChannel()], register)

    register.assert_not_called()
    profile = workspaces["slack:CADMIN"]
    assert profile.is_admin is True
    assert profile.security.contains_secrets is True
