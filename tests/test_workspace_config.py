"""Tests for workspace configuration helpers backed by Settings."""

from __future__ import annotations

import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pluggy
import pytest
import tomlkit
from conftest import make_settings

from pynchy.config import WorkspaceConfig
from pynchy.config.models import ProfileConfig, SandboxProfileConfig
from pynchy.host.orchestrator.workspace_config import (
    _ensure_toml_table,  # allow: private-test-imports - direct type contract
    add_workspace_to_toml,
    configure_plugin_workspaces,
    get_repo_access,
    get_repo_access_groups,
    load_workspace_config,
    reconcile_workspaces,
)
from pynchy.types import InboundFetchResult, OutboundEvent


class _FakePM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, hook: SimpleNamespace) -> None:
        self.hook = hook


class _FakeChannel:
    name = "connection.slack.main"
    formatter = object()

    def __init__(self, jid: str) -> None:
        self.jid = jid

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return jid == self.jid

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        return InboundFetchResult(messages=[])

    async def resolve_chat_jid(self, chat_name: str) -> str:
        return self.jid


def _settings_with_workspaces(
    *,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    defaults: SandboxProfileConfig | None = None,
):
    return make_settings(
        workspaces=workspaces or {},
        sandbox_universal=defaults or SandboxProfileConfig(),
    )


class TestLoadWorkspaceConfig:
    def teardown_method(self):
        configure_plugin_workspaces(None)

    def test_returns_none_when_missing(self):
        s = _settings_with_workspaces(workspaces={})
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert load_workspace_config("missing") is None

    def test_load_workspace_config_does_not_apply_inherited_defaults(self):
        s = _settings_with_workspaces(
            workspaces={"team": WorkspaceConfig(name="test", is_admin=False)},
            defaults=SandboxProfileConfig(trigger="always", context_mode="isolated"),
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("team")

        assert cfg is not None
        assert cfg.trigger is None  # trigger cascaded in resolve_channel_config, not here
        assert cfg.context_mode is None
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
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("daily")

        assert cfg is not None
        assert cfg.is_admin is True
        assert cfg.repo_access == "owner/pynchy"
        assert cfg.name == "Daily Agent"
        assert cfg.is_periodic is True

    def test_loads_workspace_from_plugin_spec(self):
        s = _settings_with_workspaces(workspaces={})
        fake_pm = _FakePM(
            SimpleNamespace(
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

        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("code-improver")

        assert cfg is not None
        assert cfg.repo_access == "owner/repo"
        assert cfg.is_periodic is True


class TestWorkspaceConfigModel:
    def test_defaults(self):
        cfg = WorkspaceConfig(name="test")
        assert cfg.is_admin is None
        assert cfg.trigger is None
        assert cfg.repo_access is None
        assert cfg.context_mode is None
        assert cfg.is_periodic is False

    def test_is_periodic(self):
        assert WorkspaceConfig(name="test", schedule="0 9 * * *", prompt="x").is_periodic is True
        assert WorkspaceConfig(name="test", schedule="0 9 * * *").is_periodic is False


class TestAddWorkspaceToToml:
    def test_rejects_workspace_that_does_not_round_trip_through_settings(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.chdir(tmp_path)
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            """
[profiles.worker]

[connection.slack.synapse]
bot_token_env = "SLACK_BOT_TOKEN"
app_token_env = "SLACK_APP_TOKEN"

[connection.slack.synapse.chat.daily]
"""
        )

        with pytest.raises(ValueError, match=r"workspaces\.daily\.profile is required"):
            add_workspace_to_toml(
                "daily",
                WorkspaceConfig(chat="connection.slack.synapse.chat.daily"),
            )

        assert "workspaces.daily" not in toml_path.read_text()

    def test_writes_discord_workspace_as_typed_nested_chat_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        toml_path = tmp_path / "config.toml"
        toml_path.write_text(
            """
[profiles.pynchy-dev]
is_admin = true

[connection.discord.mybot]
bot_token_env = "DISCORD_BOT_TOKEN"
dm_policy = "allowlist"
allow_from = ["ricardo"]
group_policy = "allowlist"
"""
        )

        add_workspace_to_toml(
            "discord-admin",
            WorkspaceConfig(
                profile="pynchy-dev",
                chat="connection.discord.mybot.chat.synapse.channels.code-improver",
            ),
        )

        data = tomllib.loads(toml_path.read_text())
        channel = data["connection"]["discord"]["mybot"]["chat"]["synapse"]["channels"][
            "code-improver"
        ]
        assert channel["enabled"] is True
        assert data["workspaces"]["discord-admin"]["profile"] == "pynchy-dev"
        assert data["workspaces"]["discord-admin"]["chat"] == (
            "connection.discord.mybot.chat.synapse.channels.code-improver"
        )


class TestEnsureTomlTable:
    def test_rejects_existing_non_table_value(self):
        doc = tomlkit.document()
        doc.add("section", "not-a-table")

        with pytest.raises(TypeError, match="Expected TOML table at 'section'"):
            _ensure_toml_table(doc, "section")


class TestGetRepoAccess:
    def test_returns_none_when_no_config(self):
        s = _settings_with_workspaces(workspaces={})
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert get_repo_access("dev") is None

    def test_returns_slug_from_config(self):
        s = _settings_with_workspaces(
            workspaces={"dev": WorkspaceConfig(name="test", repo_access="owner/myrepo")}
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert get_repo_access("dev") == "owner/myrepo"

    def test_admin_without_explicit_repo_access_returns_none(self):
        """Admin groups no longer get implicit repo access."""
        s = _settings_with_workspaces(
            workspaces={"admin-1": WorkspaceConfig(name="test", is_admin=True)}
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            assert get_repo_access("admin-1") is None


class TestGetRepoAccessGroups:
    def test_maps_slug_to_folders(self):
        s = _settings_with_workspaces(
            workspaces={
                "code-improver": WorkspaceConfig(name="test", repo_access="owner/pynchy"),
                "plain": WorkspaceConfig(name="test", repo_access=None),
                "other-project": WorkspaceConfig(name="test", repo_access="owner/pynchy"),
            }
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            result = get_repo_access_groups(["code-improver", "plain", "other-project"])

        assert "owner/pynchy" in result
        assert set(result["owner/pynchy"]) == {"code-improver", "other-project"}
        assert "plain" not in str(result)


@pytest.mark.asyncio
async def test_reconcile_registers_profile_admin_and_contains_secrets():
    s = make_settings(
        profiles={"admin": ProfileConfig(is_admin=True, contains_secrets=True)},
        workspaces={
            "admin": WorkspaceConfig(
                profile="admin",
                chat="connection.slack.main.chat.admin",
            )
        },
    )
    channel = _FakeChannel("slack:CADMIN")
    register = AsyncMock()

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s),
        patch(
            "pynchy.host.orchestrator.workspace_config.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await reconcile_workspaces({}, [channel], register)

    profile = register.await_args.args[0]
    assert profile.is_admin is True
    assert profile.security.contains_secrets is True
