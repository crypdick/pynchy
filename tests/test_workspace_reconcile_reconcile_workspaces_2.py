"""Tests for workspace reconciliation logic.

Tests reconcile_workspaces() which reads workspace configs from config.toml and
ensures scheduled tasks and chat groups are created. This is critical startup
logic — bugs here mean periodic agents silently don't run or get double-scheduled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import NullChannel, init_test_database, make_settings

from pynchy.conversation.api import dynamic_thread_folder
from pynchy.host.orchestrator.workspace_config import (
    configure_plugin_workspaces,
    reconcile_workspaces,
)
from pynchy.identifiers import GroupFolder, SessionId
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import create_task, get_all_tasks, set_session
from pynchy.workspace.api import WorkspaceProfile
from tests.workspace_reconcile_support import (
    _FakePM,
    _WorkspaceHarness,
    _WorkspaceSpecHooks,
    _write_workspace_yaml,
)


class _ArchivedChannel(NullChannel):
    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:")

    async def conversation_exists(self, jid: str) -> bool:
        return False


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
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
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
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
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
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
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

    @pytest.mark.parametrize(
        ("task_status", "retained"),
        [("active", True), ("paused", True), ("completed", False), ("cancelled", False)],
    )
    async def test_orphan_workspace_retention_follows_task_status(
        self, db, groups_dir, task_status, retained
    ):
        orphan_jid = "orphan@g.us"
        await create_task(
            ScheduledTask(
                id="scheduled-work",
                group_folder="orphan-agent",
                chat_jid=orphan_jid,
                prompt="Resume the work.",
                schedule_type="once",
                schedule_value="2025-01-01T04:00:00+00:00",
                session_policy=SessionPolicy.CONTINUE,
                status=task_status,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        registered = {
            orphan_jid: WorkspaceProfile(
                jid=orphan_jid,
                name="Orphan",
                folder="orphan-agent",
                trigger="@Pynchy",
                added_at=datetime.now(UTC).isoformat(),
            ),
        }

        unregister_fn = AsyncMock()
        await reconcile_workspaces(registered, [], AsyncMock(), unregister_fn=unregister_fn)

        if retained:
            unregister_fn.assert_not_awaited()
        else:
            unregister_fn.assert_awaited_once_with(orphan_jid)

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
            _WorkspaceSpecHooks(
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

    async def test_archived_child_session_does_not_block_retirement(self, db, groups_dir):
        _write_workspace_yaml(groups_dir, "support", {})
        parent = WorkspaceProfile(
            jid="discord:channel:support",
            name="Support",
            folder="support",
            trigger="@pynchy",
        )
        archived = WorkspaceProfile(
            jid="discord:channel:archived",
            name="Support/Archived",
            folder=dynamic_thread_folder(parent.folder, "discord:channel:archived"),
            trigger="@pynchy",
        )
        await set_session(GroupFolder(archived.folder), SessionId("session-1"))
        unregister = AsyncMock()
        retire = AsyncMock()

        await reconcile_workspaces(
            {parent.jid: parent, archived.jid: archived},
            [_ArchivedChannel()],
            AsyncMock(),
            unregister,
            retire_fn=retire,
        )

        retire.assert_awaited_once_with(archived.folder)
        unregister.assert_awaited_once_with(archived.jid)
