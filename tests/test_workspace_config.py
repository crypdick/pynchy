"""Tests for workspace configuration helpers backed by composable Settings."""

from __future__ import annotations

import tomllib
from collections.abc import Callable  # noqa: TC003 - dataclass field type.
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pluggy
import pytest
from conftest import make_settings
from pydantic import ValidationError

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config.api import (
    BuiltinTool,
    PipelineConfig,
    PipelineStageConfig,
    ProfileConfig,
    PromptConfig,
    WorkspaceConfig,
)
from pynchy.conversation.api import dynamic_thread_folder
from pynchy.conversation.models import ConversationId
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspacePolicy,
    configure_plugin_workspaces,
    ensure_runtime_workspace_policy_owner,
    get_repo_access_groups,
    load_resolved_config,
    load_workspace_config,
    prompt_ids_for_context,
    reconcile_workspaces,
    register_runtime_workspace_policy,
    update_profile_skill_policy,
)
from pynchy.plugins.api import (
    InboundFetchResult,
    OutboundEvent,
    WorkspaceSpec,
)
from pynchy.scheduling.api import ScheduledTask, SessionPolicy
from pynchy.workspace.api import (
    CapabilityRule,
    WorkspaceProfile,
    WorkspaceSecurity,
)


@dataclass
class _WorkspaceSpecHooks:
    """The pluggy hook subset used to collect plugin workspace specifications."""

    pynchy_workspace_spec: Callable[[], list[object]]


class _FakePM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, hook: _WorkspaceSpecHooks) -> None:
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


class _PresenceChannel(_FakeChannel):
    def __init__(self, presence: dict[str, bool | Exception]) -> None:
        self.presence = presence
        self.checked: list[str] = []

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:")

    async def conversation_exists(self, jid: str) -> bool:
        self.checked.append(jid)
        result = self.presence[jid]
        if isinstance(result, Exception):
            raise result
        return result


def _settings_with_workspaces(
    *,
    profiles: dict[str, ProfileConfig] | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
):
    configured_profiles = profiles or {}
    return make_settings(
        tools={
            tool_name: BuiltinTool(type="builtin")
            for profile in configured_profiles.values()
            for tool_name in profile.tools
        },
        profiles=configured_profiles,
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
            _WorkspaceSpecHooks(
                pynchy_workspace_spec=lambda: [
                    WorkspaceSpec(
                        folder="code-improver",
                        config=WorkspaceConfig(profiles=["worker"]).model_dump(),
                    )
                ]
            )
        )
        configure_plugin_workspaces(fake_pm)

        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            cfg = load_workspace_config("code-improver")

        assert cfg is not None
        assert cfg.profiles == ["worker"]


class TestLoadResolvedConfig:
    def teardown_method(self):
        workspace_config.clear_runtime_workspace_policies()

    def test_resolves_selected_profiles(self):
        s = _settings_with_workspaces(
            profiles={
                "base": ProfileConfig(repo=["owner/base"]),
                "dev": ProfileConfig(includes=["base"], repo=["owner/dev"], is_admin=True),
            },
            workspaces={
                "dev": WorkspaceConfig(
                    profiles=["dev"],
                    soul="souls/default",
                    pipeline="software-delivery",
                )
            },
        )
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            resolved = load_resolved_config("dev")

        assert resolved is not None
        assert resolved.repo == ["owner/base", "owner/dev"]
        assert resolved.is_admin is True
        assert resolved.soul == "souls/default"
        assert resolved.pipeline == "software-delivery"

    def test_selects_executor_from_workspace_pipeline_stage(self):
        s = make_settings(
            prompts=PromptConfig(default_pipeline="direct"),
            pipelines={
                "direct": PipelineConfig(
                    stages=[
                        PipelineStageConfig(
                            name="interactive",
                            executor="executors/default",
                        )
                    ]
                ),
                "delivery": PipelineConfig(
                    stages=[
                        PipelineStageConfig(
                            name="interactive",
                            executor="executors/default",
                        ),
                        PipelineStageConfig(
                            name="planning",
                            executor="executors/planning",
                        ),
                    ]
                ),
            },
            workspaces={
                "dev": WorkspaceConfig(
                    soul="souls/default",
                    pipeline="delivery",
                )
            },
        )

        resolved = s.resolved_workspace_config("dev")
        assert prompt_ids_for_context(
            resolved,
            "external:linear:ready_for_planning",
            settings=s,
        ) == ("souls/default", "executors/default", "executors/planning")
        assert prompt_ids_for_context(
            resolved,
            "hidden:pipeline-review:reviewers/security",
            settings=s,
        ) == ("souls/default", "reviewers/security")

    def test_runtime_policy_rejects_weakening_explicit_permissions(self):
        s = _settings_with_workspaces(
            profiles={
                "base": ProfileConfig(
                    tools=["matrix_route_read", "matrix_route_send"],
                    permissions={"deny": ["chat.matrix.*"]},
                )
            },
            workspaces={"support": WorkspaceConfig(profiles=["base"])},
        )
        register_runtime_workspace_policy(
            "support-conversation-conv_test",
            RuntimeWorkspacePolicy(
                parent_workspace="support",
                tools=("matrix_route_read",),
                capabilities={
                    "chat.matrix.route.read": CapabilityRule(decision="needs_human"),
                    "chat.matrix.route.send": CapabilityRule(decision="allow"),
                },
            ),
        )
        with (
            patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s),
            pytest.raises(ValueError, match="cannot widen explicit"),
        ):
            load_resolved_config("support-conversation-conv_test")

    def test_runtime_policy_rejects_a_different_policy_owner(self):
        register_runtime_workspace_policy(
            "support-conversation-conv_test",
            RuntimeWorkspacePolicy(parent_workspace="support"),
        )

        with pytest.raises(ValueError, match="different policy owner"):
            ensure_runtime_workspace_policy_owner("support-conversation-conv_test", "other")

    def test_stale_routed_workspace_cannot_inherit_parent_tools(self):
        s = _settings_with_workspaces(
            profiles={
                "base": ProfileConfig(
                    tools=["matrix_route_read", "matrix_route_send", "dangerous_parent_tool"]
                )
            },
            workspaces={"support": WorkspaceConfig(profiles=["base"])},
        )

        folder = routed_conversation_folder("support", ConversationId("conv_stale"))
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s):
            resolved = load_resolved_config(folder)

        assert resolved is None


def test_pipeline_context_maps_linear_sources_and_rejects_bad_reviewers():
    settings = make_settings(
        prompts=PromptConfig(default_pipeline="delivery"),
        pipelines={
            "delivery": PipelineConfig(
                stages=[
                    PipelineStageConfig(name=name, executor=f"executors/{name}")
                    for name in ("interactive", "planning", "delivery", "follow-up")
                ]
            )
        },
    )

    for source, expected in (
        ("external:linear:authorized", "delivery"),
        ("external:linear:follow-ups", "follow-up"),
        ("external:other", "interactive"),
    ):
        assert f"executors/{expected}" in prompt_ids_for_context(None, source, settings=settings)

    with pytest.raises(ValueError, match="reviewers/ scope"):
        prompt_ids_for_context(None, "hidden:pipeline-review:executors/default", settings=settings)


class TestWorkspaceConfigModel:
    def test_defaults(self):
        cfg = WorkspaceConfig()
        assert cfg.profiles == []

    def test_rejects_deleted_workspace_fields(self):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            WorkspaceConfig(profile="old")


def test_update_profile_skill_policy_persists_grants_and_denials(tmp_path, monkeypatch):
    defaults_path = tmp_path / "data" / "defaults" / "pynchy.toml"
    defaults_path.parent.mkdir(parents=True)
    defaults_path.write_text("", encoding="utf-8")
    toml_path = tmp_path / "data" / "personalization" / "pynchy.toml"
    toml_path.parent.mkdir()
    toml_path.write_text(
        """
[profiles.pynchy-dev]
skills = ["core"]
denied_skills = ["blocked-skill"]

[workspaces.pynchy-dev]
profiles = ["pynchy-dev"]
"""
    )
    settings = make_settings(project_root=tmp_path)
    monkeypatch.setattr(workspace_config, "get_settings", lambda: settings)
    monkeypatch.setattr(workspace_config, "reset_settings", lambda: None)

    update_profile_skill_policy("pynchy-dev", "obsidian-knowledge", grant=True)
    update_profile_skill_policy("pynchy-dev", "blocked-skill", grant=True)
    update_profile_skill_policy("pynchy-dev", "pynchy-operations", grant=False)

    data = tomllib.loads(toml_path.read_text())
    profile = data["profiles"]["pynchy-dev"]
    assert profile["skills"] == ["core", "obsidian-knowledge", "blocked-skill"]
    assert profile["denied_skills"] == ["pynchy-operations"]


def test_update_profile_skill_policy_rejects_unknown_profile(tmp_path, monkeypatch):
    toml_path = tmp_path / "data" / "personalization" / "pynchy.toml"
    toml_path.parent.mkdir(parents=True)
    toml_path.write_text("[profiles.existing]\n")
    settings = make_settings(project_root=tmp_path)
    monkeypatch.setattr(workspace_config, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="Profile 'missing' is not configured"):
        update_profile_skill_policy("missing", "skill", grant=True)


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
async def test_reconcile_skips_workspace_without_resolved_policy():
    settings = make_settings(
        profiles={"support": ProfileConfig()},
        workspaces={"support": WorkspaceConfig(profiles=["support"])},
    )

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=None,
        ),
        patch(
            "pynchy.host.orchestrator.workspace_config.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_config.reconcile_workspace_threads",
            new_callable=AsyncMock,
            return_value=[object()],
        ),
    ):
        await reconcile_workspaces({}, [], AsyncMock())


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


@pytest.mark.asyncio
async def test_reconcile_prunes_stale_routed_registration_for_runtime_recreation():
    s = make_settings(
        profiles={"support": ProfileConfig()},
        workspaces={"support": WorkspaceConfig(profiles=["support"])},
    )
    parent = WorkspaceProfile(
        jid="discord:channel:support",
        name="Support",
        folder="support",
        trigger="@pynchy",
    )
    stale = WorkspaceProfile(
        jid="discord:channel:stale-route",
        name="Support/Old route",
        folder=routed_conversation_folder("support", ConversationId("conv_stale")),
        trigger="@pynchy",
    )
    workspaces = {parent.jid: parent, stale.jid: stale}
    unregister = AsyncMock()

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s),
        patch(
            "pynchy.host.orchestrator.workspace_config.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_all_sessions",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "pynchy.host.orchestrator.workspace_registration.set_workspace_profile",
            new_callable=AsyncMock,
        ),
    ):
        await reconcile_workspaces(
            workspaces,
            [_FakeChannel()],
            AsyncMock(),
            unregister,
        )

    unregister.assert_awaited_once_with(stale.jid)


@pytest.mark.asyncio
async def test_reconcile_prunes_only_provider_deleted_unowned_children():
    s = make_settings(
        profiles={"support": ProfileConfig()},
        workspaces={"support": WorkspaceConfig(profiles=["support"])},
    )
    parent = WorkspaceProfile(
        jid="discord:channel:support",
        name="Support",
        folder="support",
        trigger="@pynchy",
    )

    def child(suffix: str) -> WorkspaceProfile:
        jid = f"discord:channel:{suffix}"
        return WorkspaceProfile(
            jid=jid,
            name=f"Support/{suffix}",
            folder=dynamic_thread_folder(parent.folder, jid),
            trigger="@pynchy",
        )

    deleted = child("deleted")
    live = child("live")
    unknown = child("unknown")
    task_owned = child("task")
    session_owned = child("session")
    workspaces = {
        profile.jid: profile
        for profile in (parent, deleted, live, unknown, task_owned, session_owned)
    }
    channel = _PresenceChannel(
        {
            deleted.jid: False,
            live.jid: True,
            unknown.jid: OSError("offline"),
            session_owned.jid: True,
        }
    )
    task = ScheduledTask(
        id="task-owner",
        group_folder=parent.folder,
        chat_jid=parent.jid,
        prompt="",
        schedule_type="interval",
        schedule_value="1h",
        session_policy=SessionPolicy.CONTINUE,
        bound_group_folder=task_owned.folder,
        bound_chat_jid=task_owned.jid,
    )
    unregister = AsyncMock()
    retire = AsyncMock()

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=s),
        patch(
            "pynchy.host.orchestrator.workspace_config.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[task],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_all_tasks",
            new_callable=AsyncMock,
            return_value=[task],
        ),
        patch(
            "pynchy.host.orchestrator.workspace_artifacts.get_all_sessions",
            new_callable=AsyncMock,
            return_value={session_owned.folder: "session-1"},
        ),
        patch(
            "pynchy.host.orchestrator.workspace_registration.set_workspace_profile",
            new_callable=AsyncMock,
        ),
    ):
        await reconcile_workspaces(
            workspaces,
            [channel],
            AsyncMock(),
            unregister,
            retire_fn=retire,
        )

    unregister.assert_awaited_once_with(deleted.jid)
    retire.assert_awaited_once_with(deleted.folder)
    assert channel.checked == [deleted.jid, live.jid, unknown.jid, session_owned.jid]
