"""Tests for workspace reconciliation logic.

Tests reconcile_workspaces() which reads workspace configs from config.toml and
ensures scheduled tasks and chat groups are created. This is critical startup
logic — bugs here mean periodic agents silently don't run or get double-scheduled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

from pynchy.config import CommandCenterConfig, WorkspaceConfig
from pynchy.db import _init_test_database, create_task, get_active_task_for_group, get_all_tasks
from pynchy.types import WorkspaceProfile
from pynchy.workspace_config import (
    configure_plugin_workspaces,
    reconcile_workspaces,
)


def _write_workspace_yaml(workspaces, folder_name, data):
    """Compat helper: populate Settings.workspaces for tests."""
    d = data or {}
    d.setdefault("name", folder_name)
    workspaces[folder_name] = WorkspaceConfig.model_validate(d)


class TestReconcileWorkspaces:
    """Tests for reconcile_workspaces() startup reconciliation."""

    @pytest.fixture
    async def db(self):
        await _init_test_database()

    @pytest.fixture
    def groups_dir(self, monkeypatch, tmp_path):
        workspaces: dict[str, WorkspaceConfig] = {}
        s = make_settings(workspaces=workspaces, groups_dir=tmp_path / "groups")
        monkeypatch.setattr("pynchy.workspace_config.get_settings", lambda: s)
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
            {
                "id": "periodic-monitor-abc123",
                "group_folder": "monitor",
                "chat_jid": "monitor@g.us",
                "prompt": "Monitor systems",
                "schedule_type": "cron",
                "schedule_value": "*/15 * * * *",  # OLD schedule
                "context_mode": "group",
                "next_run": "2025-01-01T00:15:00",
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
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
            {
                "id": "periodic-monitor-abc123",
                "group_folder": "monitor",
                "chat_jid": "monitor@g.us",
                "prompt": "Old monitoring prompt",  # OLD prompt
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "context_mode": "group",
                "next_run": "2025-01-01T09:00:00",
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
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
            {
                "id": "periodic-monitor-abc123",
                "group_folder": "monitor",
                "chat_jid": "monitor@g.us",
                "prompt": "Monitor systems",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "context_mode": "group",
                "next_run": "2025-01-01T09:00:00",
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
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

        # Should still have exactly 1 task, unchanged
        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].id == "periodic-monitor-abc123"

    async def test_creates_chat_group_for_unregistered_workspace(
        self, db, monkeypatch, tmp_path
    ):
        """Workspace with no DB entry should create a chat group via channel."""
        conn_ref = "connection.whatsapp.main"
        chat_ref = f"{conn_ref}.chat.new-agent"
        workspaces: dict[str, WorkspaceConfig] = {}
        s = make_settings(
            workspaces=workspaces,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "new-agent",
            {
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
                "trigger": "always",
                "chat": chat_ref,
            },
        )

        mock_channel = AsyncMock()
        mock_channel.name = conn_ref
        mock_channel.resolve_chat_jid = AsyncMock(return_value=None)
        mock_channel.create_group = AsyncMock(return_value="new-agent@g.us")

        registered: dict[str, WorkspaceProfile] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        # Should have called create_group with the chat name
        mock_channel.create_group.assert_called_once_with("new-agent")
        # Should have registered the group
        register_fn.assert_called_once()
        profile = register_fn.call_args[0][0]
        assert profile.jid == "new-agent@g.us"
        assert profile.folder == "new-agent"
        assert profile.trigger is not None  # trigger is the @mention string

    async def test_skips_when_no_channel_supports_create_group(
        self, db, monkeypatch, tmp_path
    ):
        """Workspace needing new group should be skipped if no channel supports it."""
        conn_ref = "connection.whatsapp.main"
        chat_ref = f"{conn_ref}.chat.orphan-agent"
        workspaces: dict[str, WorkspaceConfig] = {}
        s = make_settings(
            workspaces=workspaces,
            groups_dir=tmp_path / "groups",
            command_center=CommandCenterConfig(connection=conn_ref),
        )
        monkeypatch.setattr("pynchy.workspace_config.get_settings", lambda: s)

        _write_workspace_yaml(
            workspaces,
            "orphan-agent",
            {
                "schedule": "0 9 * * *",
                "prompt": "Check things",
                "chat": chat_ref,
            },
        )

        # Channel matches connection but lacks create_group
        mock_channel = AsyncMock(spec=["send_message", "connect", "disconnect"])
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

    async def test_repo_access_preserved_in_task(self, db, groups_dir):
        """repo_access from config.toml should be set on the created task."""
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
        assert tasks[0].repo_access == "owner/pynchy"

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

    async def test_pauses_orphaned_task_when_workspace_removed(self, db, groups_dir):
        """Task for a removed workspace should be paused on reconciliation."""
        # Pre-seed a task for a workspace that no longer exists in config
        await create_task(
            {
                "id": "periodic-old-agent-abc123",
                "group_folder": "old-agent",
                "chat_jid": "old@g.us",
                "prompt": "Do old things",
                "schedule_type": "cron",
                "schedule_value": "0 4 * * *",
                "context_mode": "isolated",
                "next_run": "2025-01-01T04:00:00",
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
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
            {
                "id": "periodic-was-periodic-abc123",
                "group_folder": "was-periodic",
                "chat_jid": "was@g.us",
                "prompt": "Old prompt",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "context_mode": "isolated",
                "next_run": "2025-01-01T09:00:00",
                "status": "active",
                "created_at": datetime.now(UTC).isoformat(),
            }
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
            {
                "id": "periodic-gone-abc123",
                "group_folder": "gone-agent",
                "chat_jid": "gone@g.us",
                "prompt": "Gone",
                "schedule_type": "cron",
                "schedule_value": "0 4 * * *",
                "context_mode": "isolated",
                "next_run": "2025-01-01T04:00:00",
                "status": "paused",  # already paused
                "created_at": datetime.now(UTC).isoformat(),
            }
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

    async def test_plugin_workspace_creates_task(self, db, groups_dir, tmp_path):
        fake_pm = SimpleNamespace(
            hook=SimpleNamespace(
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
        assert len(tasks) == 1
        assert tasks[0].group_folder == "code-improver"
        assert tasks[0].repo_access == "owner/pynchy"
