"""Tests for workspace reconciliation logic.

Tests reconcile_workspaces() which scans groups/ for workspace.yaml files and
ensures scheduled tasks and chat groups are created. This is critical startup
logic — bugs here mean periodic agents silently don't run or get double-scheduled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from conftest import make_settings

from pynchy.config import WorkspaceConfig
from pynchy.db import _init_test_database, create_task, get_active_task_for_group, get_all_tasks
from pynchy.types import RegisteredGroup
from pynchy.workspace_config import configure_plugin_workspaces, reconcile_workspaces


def _write_workspace_yaml(workspaces, folder_name, data):
    """Compat helper: populate Settings.workspaces for tests."""
    workspaces[folder_name] = WorkspaceConfig.model_validate(data or {})


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
        """Periodic workspace.yaml should create a scheduled task."""
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
            "improver@g.us": RegisteredGroup(
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
                "is_god": False,
                "requires_trigger": True,
            },
        )

        registered = {
            "regular@g.us": RegisteredGroup(
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
        """Changed schedule in workspace.yaml should update existing task."""
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
            "monitor@g.us": RegisteredGroup(
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
        """Changed prompt in workspace.yaml should update existing task."""
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
            "monitor@g.us": RegisteredGroup(
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
            "monitor@g.us": RegisteredGroup(
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

    async def test_creates_chat_group_for_unregistered_workspace(self, db, groups_dir):
        """Workspace with no DB entry should create a chat group via channel."""
        _write_workspace_yaml(
            groups_dir,
            "new-agent",
            {
                "schedule": "0 8 * * 1",
                "prompt": "Weekly report",
                "requires_trigger": False,
            },
        )

        mock_channel = AsyncMock()
        mock_channel.create_group = AsyncMock(return_value="new-agent@g.us")

        registered: dict[str, RegisteredGroup] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        # Should have called create_group
        mock_channel.create_group.assert_called_once()
        # Should have registered the group
        register_fn.assert_called_once()
        call_args = register_fn.call_args
        assert call_args[0][0] == "new-agent@g.us"
        group = call_args[0][1]
        assert group.folder == "new-agent"
        assert group.requires_trigger is False

    async def test_skips_when_no_channel_supports_create_group(self, db, groups_dir):
        """Workspace needing new group should be skipped if no channel supports it."""
        _write_workspace_yaml(
            groups_dir,
            "orphan-agent",
            {
                "schedule": "0 9 * * *",
                "prompt": "Check things",
            },
        )

        # Channel without create_group attribute
        mock_channel = AsyncMock(spec=["send_message", "connect", "disconnect"])

        registered: dict[str, RegisteredGroup] = {}
        register_fn = AsyncMock()

        await reconcile_workspaces(registered, [mock_channel], register_fn)

        # Should not have registered anything
        register_fn.assert_not_called()
        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_empty_groups_dir(self, db, groups_dir):
        """Empty groups directory should not crash."""
        registered: dict[str, RegisteredGroup] = {}
        register_fn = AsyncMock()

        # Should not raise
        await reconcile_workspaces(registered, [], register_fn)

    async def test_nonexistent_groups_dir(self, db):
        """No configured workspaces should not crash."""
        registered: dict[str, RegisteredGroup] = {}
        register_fn = AsyncMock()

        # Should not raise
        await reconcile_workspaces(registered, [], register_fn)

    async def test_project_access_preserved_in_task(self, db, groups_dir):
        """project_access from workspace.yaml should be set on the created task."""
        _write_workspace_yaml(
            groups_dir,
            "dev-agent",
            {
                "schedule": "0 4 * * *",
                "prompt": "Run improvements",
                "project_access": True,
            },
        )

        registered = {
            "dev@g.us": RegisteredGroup(
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
        assert tasks[0].project_access is True

    async def test_context_mode_preserved_in_task(self, db, groups_dir):
        """context_mode from workspace.yaml should be set on the created task."""
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
            "iso@g.us": RegisteredGroup(
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

    async def test_plugin_workspace_creates_task_and_seeds_claude_file(
        self, db, groups_dir, tmp_path
    ):
        fake_pm = SimpleNamespace(
            hook=SimpleNamespace(
                pynchy_workspace_spec=lambda: [
                    {
                        "folder": "code-improver",
                        "config": {
                            "project_access": True,
                            "schedule": "0 4 * * *",
                            "prompt": "Run code improvements",
                            "context_mode": "isolated",
                        },
                        "claude_md": "# Code Improver\\n\\nPlugin managed.",
                    }
                ]
            )
        )
        configure_plugin_workspaces(fake_pm)

        registered = {
            "improver@g.us": RegisteredGroup(
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
        assert tasks[0].project_access is True
        claude_path = tmp_path / "groups" / "code-improver" / "CLAUDE.md"
        assert claude_path.exists()
