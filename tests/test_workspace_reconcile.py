"""Tests for workspace reconciliation logic.

Tests reconcile_workspaces() which reads workspace configs from config.toml and
ensures scheduled tasks and chat groups are created. This is critical startup
logic — bugs here mean periodic agents silently don't run or get double-scheduled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pluggy
import pytest
from conftest import init_test_database, make_settings

from pynchy.config import CommandCenterConfig, WorkspaceConfig
from pynchy.config.jobs import JobConfig
from pynchy.config.models import ProfileConfig
from pynchy.host.orchestrator.workspace_config import (
    configure_plugin_workspaces,
    dynamic_thread_folder,
    reconcile_workspaces,
)
from pynchy.state import create_task, get_active_task_for_group, get_all_tasks
from pynchy.types import Channel, ScheduledTask, WorkspaceProfile


class _FakePM(pluggy.PluginManager):
    """Real-class stand-in so isinstance(pm, pluggy.PluginManager) succeeds."""

    def __init__(self, hook: SimpleNamespace) -> None:
        self.hook = hook


class _WorkspaceHarness(dict[str, WorkspaceConfig]):
    def __init__(self) -> None:
        super().__init__()
        self.profiles: dict[str, ProfileConfig] = {}
        self.jobs: dict[str, JobConfig] = {}


def _write_workspace_yaml(workspaces, folder_name, data):
    """Compat helper: populate Settings.workspaces for tests."""
    d = data or {}
    profile_name = f"{folder_name}-profile"
    profile = ProfileConfig(
        is_admin=bool(d.get("is_admin", False)),
        repo=d.get("repo_access", []),
    )
    if isinstance(workspaces, _WorkspaceHarness):
        workspaces.profiles[profile_name] = profile
        if "schedule" in d and "prompt" in d:
            workspaces.jobs[folder_name] = JobConfig(
                workspace=folder_name,
                schedule=d["schedule"],
                prompt=d["prompt"],
            )
    workspaces[folder_name] = WorkspaceConfig(profiles=[profile_name])


class TestReconcileWorkspaces:
    """Tests for reconcile_workspaces() startup reconciliation."""

    @pytest.fixture
    async def db(self):
        await init_test_database()

    @pytest.fixture
    def groups_dir(self, monkeypatch, tmp_path):
        workspaces = _WorkspaceHarness()
        s = make_settings(
            profiles=workspaces.profiles,
            workspaces=workspaces,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)
        return workspaces

    @pytest.fixture(autouse=True)
    def reset_plugin_workspaces(self):
        configure_plugin_workspaces(None)
        yield
        configure_plugin_workspaces(None)

    async def test_creates_task_for_periodic_workspace(self, db, groups_dir):
        """Periodic workspace config should create a scheduled task."""
        _write_workspace_yaml(
            groups_dir,
            "code-improver",
            {
                "schedule": "0 4 * * *",
                "prompt": "Run code improvements",
            },
        )

        # Pre-register the group (simulating it already exists)
        registered = {
            "improver@g.us": WorkspaceProfile(
                jid="improver@g.us",
                name="Code Improver",
                folder="code-improver",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].group_folder == "code-improver"
        assert tasks[0].schedule_type == "cron"
        assert tasks[0].schedule_value == "0 4 * * *"
        assert tasks[0].prompt == "Run code improvements"
        assert tasks[0].status == "active"

    async def test_skips_non_periodic_workspace(self, db, groups_dir):
        """Non-periodic workspace (no schedule) should not create tasks."""
        _write_workspace_yaml(
            groups_dir,
            "regular-group",
            {
                "is_admin": False,
                "trigger": "mention",
            },
        )

        registered = {
            "regular@g.us": WorkspaceProfile(
                jid="regular@g.us",
                name="Regular",
                folder="regular-group",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_updates_task_when_schedule_changes(self, db, groups_dir):
        """Changed schedule in config.toml should update existing task."""
        _write_workspace_yaml(
            groups_dir,
            "monitor",
            {
                "schedule": "*/30 * * * *",
                "prompt": "Monitor systems",
            },
        )

        # Create existing task with old schedule
        await create_task(
            ScheduledTask(
                id="job-monitor",
                group_folder="monitor",
                chat_jid="monitor@g.us",
                prompt="Monitor systems",
                schedule_type="cron",
                schedule_value="*/15 * * * *",  # OLD schedule
                context_mode="group",
                next_run="2025-01-01T00:15:00",
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        registered = {
            "monitor@g.us": WorkspaceProfile(
                jid="monitor@g.us",
                name="Monitor",
                folder="monitor",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        task = await get_active_task_for_group("monitor")
        assert task is not None
        assert task.schedule_value == "*/30 * * * *"  # Updated

    async def test_updates_task_when_prompt_changes(self, db, groups_dir):
        """Changed prompt in config.toml should update existing task."""
        _write_workspace_yaml(
            groups_dir,
            "monitor",
            {
                "schedule": "0 9 * * *",
                "prompt": "New monitoring prompt",
            },
        )

        await create_task(
            ScheduledTask(
                id="job-monitor",
                group_folder="monitor",
                chat_jid="monitor@g.us",
                prompt="Old monitoring prompt",  # OLD prompt
                schedule_type="cron",
                schedule_value="0 9 * * *",
                context_mode="group",
                next_run="2025-01-01T09:00:00",
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        registered = {
            "monitor@g.us": WorkspaceProfile(
                jid="monitor@g.us",
                name="Monitor",
                folder="monitor",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        task = await get_active_task_for_group("monitor")
        assert task is not None
        assert task.prompt == "New monitoring prompt"

    async def test_no_update_when_nothing_changed(self, db, groups_dir):
        """Idempotent — no update when config matches existing task."""
        _write_workspace_yaml(
            groups_dir,
            "monitor",
            {
                "schedule": "0 9 * * *",
                "prompt": "Monitor systems",
            },
        )

        await create_task(
            ScheduledTask(
                id="runtime-monitor-abc123",
                group_folder="monitor",
                chat_jid="monitor@g.us",
                prompt="Monitor systems",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                context_mode="group",
                next_run="2025-01-01T09:00:00",
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        registered = {
            "monitor@g.us": WorkspaceProfile(
                jid="monitor@g.us",
                name="Monitor",
                folder="monitor",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        # The config-backed job owns its own row; unmanaged runtime tasks are left alone.
        tasks = await get_all_tasks()
        assert {task.id for task in tasks} == {"job-monitor", "runtime-monitor-abc123"}
        active = [task for task in tasks if task.status == "active"]
        assert {task.id for task in active} == {"job-monitor", "runtime-monitor-abc123"}

    async def test_creates_chat_group_for_unregistered_workspace(self, db, monkeypatch, tmp_path):
        """Workspace with no DB entry should create a chat group via channel."""
        conn_ref = "main"
        workspaces = _WorkspaceHarness()
        s = make_settings(
            workspaces=workspaces,
            profiles=workspaces.profiles,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "new-agent",
            {
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
            },
        )

        mock_channel = AsyncMock(spec=Channel)
        mock_channel.name = conn_ref
        mock_channel.create_group = AsyncMock(return_value="new-agent@g.us")

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        mock_channel.create_group.assert_awaited_once_with("New Agent")
        register_fn.assert_awaited_once()
        registered_profile = register_fn.await_args.args[0]
        assert registered_profile.jid == "new-agent@g.us"
        assert registered_profile.folder == "new-agent"
        tasks = await get_all_tasks()
        assert [task.id for task in tasks] == ["job-new-agent"]

    async def test_workspace_display_name_uses_folder_not_repo_access(
        self,
        db,
        monkeypatch,
        tmp_path,
    ):
        """Workspace channels are named by workspace, even when repos match."""
        conn_ref = "main"
        workspaces = _WorkspaceHarness()
        s = make_settings(
            workspaces=workspaces,
            profiles=workspaces.profiles,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)

        shared_repo = "get-synapse-ai/gantt-believe-it"
        _write_workspace_yaml(workspaces, "project-managing", {"repo_access": shared_repo})
        _write_workspace_yaml(workspaces, "dddd-evening-review", {"repo_access": shared_repo})

        mock_channel = AsyncMock(spec=Channel)
        mock_channel.name = conn_ref
        mock_channel.create_group = AsyncMock(
            side_effect=["project-managing@g.us", "dddd-evening-review@g.us"]
        )

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        mock_channel.create_group.assert_has_awaits(
            [call("Project Managing"), call("Dddd Evening Review")]
        )
        assert register_fn.await_count == 2
        assert {profile.folder for profile in registered.values()} == {
            "project-managing",
            "dddd-evening-review",
        }

    async def test_create_group_empty_jid_skips_workspace(
        self,
        db,
        monkeypatch,
        tmp_path,
    ):
        """An empty create_group result is invalid and should skip the workspace."""
        conn_ref = "main"
        workspaces = _WorkspaceHarness()
        s = make_settings(
            workspaces=workspaces,
            profiles=workspaces.profiles,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "new-agent",
            {
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
            },
        )

        mock_channel = AsyncMock(spec=Channel)
        mock_channel.name = conn_ref
        mock_channel.create_group = AsyncMock(return_value="")

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        mock_channel.create_group.assert_awaited_once_with("New Agent")
        register_fn.assert_not_called()

    async def test_create_group_existing_jid_for_other_workspace_skips_registration(
        self,
        db,
        monkeypatch,
        tmp_path,
    ):
        """A reused channel JID must not remap an existing workspace."""
        conn_ref = "main"
        workspaces = _WorkspaceHarness()
        s = make_settings(
            workspaces=workspaces,
            profiles=workspaces.profiles,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "general",
            {
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
            },
        )

        existing_profile = WorkspaceProfile(
            jid="discord:channel:1",
            name="Discord Admin",
            folder="discord-admin",
            trigger="@Pynchy",
            added_at=datetime.now(UTC).isoformat(),
            is_admin=True,
        )
        mock_channel = AsyncMock(spec=Channel)
        mock_channel.name = conn_ref
        mock_channel.create_group = AsyncMock(return_value=existing_profile.jid)

        registered = {existing_profile.jid: existing_profile}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        mock_channel.create_group.assert_awaited_once_with("General")
        register_fn.assert_not_called()
        assert registered[existing_profile.jid] == existing_profile
        assert await get_active_task_for_group("general") is None

    async def test_create_group_failure_skips_workspace(
        self,
        db,
        monkeypatch,
        tmp_path,
    ):
        """A channel that cannot provision the workspace should not block startup."""
        conn_ref = "main"
        workspaces = _WorkspaceHarness()
        s = make_settings(
            workspaces=workspaces,
            profiles=workspaces.profiles,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "new-agent",
            {
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
            },
        )

        mock_channel = AsyncMock(spec=Channel)
        mock_channel.name = conn_ref
        mock_channel.create_group = AsyncMock(side_effect=OSError("transport unavailable"))

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        mock_channel.create_group.assert_awaited_once_with("New Agent")
        register_fn.assert_not_called()

    async def test_discord_workspace_can_auto_create_configured_channel(
        self, db, monkeypatch, tmp_path
    ):
        """Discord config owns channel provisioning even when it is not command center."""
        conn_ref = "connection.discord.main"
        chat_ref = f"{conn_ref}.chat.synapse.channels.code-improver"
        workspaces = _WorkspaceHarness()
        s = make_settings(
            workspaces=workspaces,
            profiles=workspaces.profiles,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection="default"),
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "code-improver",
            {
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
                "trigger": "always",
                "chat": chat_ref,
            },
        )

        mock_channel = AsyncMock(spec=Channel)
        mock_channel.name = conn_ref
        mock_channel.auto_provision_configured_chats = True
        mock_channel.resolve_chat_jid = AsyncMock(return_value=None)
        mock_channel.create_group = AsyncMock(return_value="discord:channel:789")

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        mock_channel.create_group.assert_not_called()
        register_fn.assert_not_called()

    async def test_skips_when_no_channel_supports_create_group(self, db, monkeypatch, tmp_path):
        """Workspace needing new group should be skipped if no channel supports it."""
        conn_ref = "main"
        chat_ref = f"{conn_ref}.chat.orphan-agent"
        workspaces = _WorkspaceHarness()
        s = make_settings(
            workspaces=workspaces,
            profiles=workspaces.profiles,
            jobs=workspaces.jobs,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.host.orchestrator.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "orphan-agent",
            {
                "schedule": "0 9 * * *",
                "prompt": "Check things",
                "chat": chat_ref,
            },
        )

        # Channel matches connection but lacks create_group (not part of the
        # Channel protocol — spec=Channel naturally omits it).
        mock_channel = AsyncMock(spec=Channel)
        mock_channel.name = conn_ref

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        # Should not have registered anything
        register_fn.assert_not_called()
        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_empty_groups_dir(self, db, groups_dir):
        """Empty groups directory should not crash."""
        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        # Should not raise
        await reconcile_workspaces(registered, [], register_fn)

    async def test_nonexistent_groups_dir(self, db):
        """No configured workspaces should not crash."""
        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        # Should not raise
        await reconcile_workspaces(registered, [], register_fn)

    async def test_repo_access_not_copied_to_task(self, db, groups_dir):
        """Scheduled runs resolve workspace repos live instead of copying task state."""
        _write_workspace_yaml(
            groups_dir,
            "dev-agent",
            {
                "schedule": "0 4 * * *",
                "prompt": "Run improvements",
                "repo_access": "owner/pynchy",
            },
        )

        registered = {
            "dev@g.us": WorkspaceProfile(
                jid="dev@g.us",
                name="Dev Agent",
                folder="dev-agent",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].repo_access is None

    async def test_context_mode_preserved_in_task(self, db, groups_dir):
        """context_mode from config.toml should be set on the created task."""
        _write_workspace_yaml(
            groups_dir,
            "isolated-agent",
            {
                "schedule": "0 9 * * *",
                "prompt": "Isolated work",
                "context_mode": "isolated",
            },
        )

        registered = {
            "iso@g.us": WorkspaceProfile(
                jid="iso@g.us",
                name="Isolated Agent",
                folder="isolated-agent",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].context_mode == "isolated"

    async def test_updates_workspace_name_from_config(self, db, groups_dir):
        """Changed name in config.toml should update existing workspace profile."""
        _write_workspace_yaml(
            groups_dir,
            "my-agent",
            {"name": "New Name"},
        )

        registered = {
            "agent@g.us": WorkspaceProfile(
                jid="agent@g.us",
                name="Old Name",
                folder="my-agent",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        assert registered["agent@g.us"].name == "My Agent"

    async def test_updates_is_admin_from_config(self, db, groups_dir):
        """Changed is_admin in config.toml should update existing workspace profile."""
        _write_workspace_yaml(
            groups_dir,
            "promoted",
            {"is_admin": True},
        )

        registered = {
            "promoted@g.us": WorkspaceProfile(
                jid="promoted@g.us",
                name="Promoted",
                folder="promoted",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
                is_admin=False,
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        assert registered["promoted@g.us"].is_admin is True

    async def test_no_update_when_profile_matches_config(self, db, groups_dir):
        """No DB write when workspace profile already matches config."""
        _write_workspace_yaml(
            groups_dir,
            "stable",
            {"name": "Stable Agent"},
        )

        registered = {
            "stable@g.us": WorkspaceProfile(
                jid="stable@g.us",
                name="Stable Agent",
                folder="stable",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        # register_fn should NOT be called — no creation or update needed
        register_fn.assert_not_called()

    async def test_pauses_orphaned_task_when_workspace_removed(self, db, groups_dir):
        """Task for a removed workspace should be paused on reconciliation."""
        # Pre-seed a task for a workspace that no longer exists in config
        await create_task(
            ScheduledTask(
                id="periodic-old-agent-abc123",
                group_folder="old-agent",
                chat_jid="old@g.us",
                prompt="Do old things",
                schedule_type="cron",
                schedule_value="0 4 * * *",
                context_mode="isolated",
                next_run="2025-01-01T04:00:00",
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        # Config has no workspaces — the old-agent workspace was removed
        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == "paused"

    async def test_pauses_task_when_workspace_becomes_non_periodic(self, db, groups_dir):
        """Task should be paused when workspace loses its schedule."""
        # Workspace exists but is no longer periodic (no schedule)
        _write_workspace_yaml(
            groups_dir,
            "was-periodic",
            {
                "is_admin": False,
                # No schedule or prompt — not periodic anymore
            },
        )

        await create_task(
            ScheduledTask(
                id="periodic-was-periodic-abc123",
                group_folder="was-periodic",
                chat_jid="was@g.us",
                prompt="Old prompt",
                schedule_type="cron",
                schedule_value="0 9 * * *",
                context_mode="isolated",
                next_run="2025-01-01T09:00:00",
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        registered = {
            "was@g.us": WorkspaceProfile(
                jid="was@g.us",
                name="Was Periodic",
                folder="was-periodic",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == "paused"

    async def test_does_not_pause_already_paused_tasks(self, db, groups_dir):
        """Already-paused tasks should not be touched."""
        await create_task(
            ScheduledTask(
                id="periodic-gone-abc123",
                group_folder="gone-agent",
                chat_jid="gone@g.us",
                prompt="Gone",
                schedule_type="cron",
                schedule_value="0 4 * * *",
                context_mode="isolated",
                next_run="2025-01-01T04:00:00",
                status="paused",  # already paused
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].status == "paused"

    async def test_removes_orphaned_workspace_registration(self, db, groups_dir):
        """Workspace in DB but not in config should be unregistered."""
        orphan_jid = "orphan@g.us"
        registered = {
            orphan_jid: WorkspaceProfile(
                jid=orphan_jid,
                name="Orphan",
                folder="orphan-agent",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        unregister_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn, unregister_fn=unregister_fn)

        unregister_fn.assert_called_once_with(orphan_jid)

    async def test_does_not_remove_admin_workspace_without_config(self, db, groups_dir):
        """Admin workspaces are exempt — created dynamically, no config entry."""
        admin_jid = "admin@g.us"
        registered = {
            admin_jid: WorkspaceProfile(
                jid=admin_jid,
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
                is_admin=True,
            ),
        }

        register_fn = AsyncMock()
        unregister_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn, unregister_fn=unregister_fn)

        unregister_fn.assert_not_called()

    async def test_does_not_remove_workspace_present_in_config(self, db, groups_dir):
        """Workspaces with matching config should not be removed."""
        _write_workspace_yaml(
            groups_dir,
            "active-agent",
            {"is_admin": False},
        )

        registered = {
            "active@g.us": WorkspaceProfile(
                jid="active@g.us",
                name="Active",
                folder="active-agent",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        register_fn = AsyncMock()
        unregister_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn, unregister_fn=unregister_fn)

        unregister_fn.assert_not_called()

    async def test_does_not_remove_dynamic_thread_when_parent_workspace_is_configured(
        self, db, groups_dir
    ):
        """Dynamic thread registrations inherit the lifecycle of their configured parent."""
        _write_workspace_yaml(
            groups_dir,
            "research",
            {"is_admin": False},
        )
        thread_jid = "discord:channel:thread"

        registered = {
            thread_jid: WorkspaceProfile(
                jid=thread_jid,
                name="Research/thread",
                folder=dynamic_thread_folder("research", thread_jid),
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
                is_admin=False,
            ),
        }

        register_fn = AsyncMock()
        unregister_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn, unregister_fn=unregister_fn)

        unregister_fn.assert_not_called()

    async def test_invalid_plugin_workspace_config_is_ignored(self, db, groups_dir, tmp_path):
        fake_pm = _FakePM(
            SimpleNamespace(
                pynchy_workspace_spec=lambda: [
                    {
                        "folder": "code-improver",
                        "config": {
                            "name": "Code Improver",
                            "repo_access": "owner/pynchy",
                            "schedule": "0 4 * * *",
                            "prompt": "Run code improvements",
                            "context_mode": "isolated",
                        },
                    }
                ]
            )
        )
        configure_plugin_workspaces(fake_pm)

        registered = {
            "improver@g.us": WorkspaceProfile(
                jid="improver@g.us",
                name="Code Improver",
                folder="code-improver",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }
        register_fn = AsyncMock()
        await reconcile_workspaces(registered, [], register_fn)

        tasks = await get_all_tasks()
        assert tasks == []
