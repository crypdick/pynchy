"""Tests for config-backed job reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import init_test_database, make_settings

from pynchy.config.jobs import JobConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator.workspace_config import reconcile_workspaces
from pynchy.state import create_task, get_active_task_for_group, get_all_tasks
from pynchy.types import ScheduledTask, WorkspaceProfile


class TestJobReconcile:
    @pytest.fixture
    async def db(self):
        await init_test_database()

    def _patch_settings(self, monkeypatch, tmp_path, *, jobs: dict[str, JobConfig]):
        settings = make_settings(
            groups_dir=tmp_path / "groups",
            profiles={
                "admin": ProfileConfig(
                    is_admin=True,
                    repo="crypdick/pynchy",
                )
            },
            workspaces={"admin": WorkspaceConfig(profiles=["admin"])},
            jobs=jobs,
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.get_settings", lambda: settings
        )
        return settings

    async def test_agent_job_creates_scheduled_task_for_workspace(self, db, monkeypatch, tmp_path):
        self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "daily-triage": JobConfig(
                    enabled=True,
                    schedule="0 8 * * *",
                    workspace="admin",
                    prompt="Run the daily triage memo.",
                )
            },
        )
        registered = {
            "slack:CADMIN": WorkspaceProfile(
                jid="slack:CADMIN",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
            )
        }

        await reconcile_workspaces(registered, [], AsyncMock())

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.id == "job-daily-triage"
        assert task.group_folder == "admin"
        assert task.chat_jid == "slack:CADMIN"
        assert task.prompt == "Run the daily triage memo."
        assert task.schedule_type == "cron"
        assert task.schedule_value == "0 8 * * *"
        assert task.context_mode == "isolated"
        assert task.repo_access == "crypdick/pynchy"

    async def test_one_time_agent_job_creates_once_task(self, db, monkeypatch, tmp_path):
        run_at = "2026-07-08T18:30:00-07:00"
        self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "cancel-youtube-premium": JobConfig(
                    enabled=True,
                    at=run_at,
                    workspace="admin",
                    prompt="Cancel YouTube Premium.",
                )
            },
        )
        registered = {
            "slack:CADMIN": WorkspaceProfile(
                jid="slack:CADMIN",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
            )
        }

        await reconcile_workspaces(registered, [], AsyncMock())

        task = await get_active_task_for_group("admin")
        assert task is not None
        assert task.id == "job-cancel-youtube-premium"
        assert task.schedule_type == "once"
        assert task.schedule_value == run_at
        assert task.next_run == run_at

    async def test_job_reconcile_updates_delivery_when_workspace_jid_changes(
        self, db, monkeypatch, tmp_path
    ):
        self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "daily-triage": JobConfig(
                    enabled=True,
                    schedule="0 8 * * *",
                    workspace="admin",
                    prompt="Run the daily triage memo.",
                )
            },
        )
        await create_task(
            ScheduledTask(
                id="job-daily-triage",
                group_folder="admin",
                chat_jid="slack:COLD",
                prompt="Run the daily triage memo.",
                schedule_type="cron",
                schedule_value="0 8 * * *",
                context_mode="isolated",
                next_run=datetime(2026, 7, 8, 8, 0, tzinfo=UTC).isoformat(),
                status="active",
                created_at=datetime.now(UTC).isoformat(),
                repo_access="crypdick/pynchy",
            )
        )
        registered = {
            "slack:CNEW": WorkspaceProfile(
                jid="slack:CNEW",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
            )
        }

        await reconcile_workspaces(registered, [], AsyncMock())

        task = await get_active_task_for_group("admin")
        assert task is not None
        assert task.chat_jid == "slack:CNEW"

    async def test_disabled_job_pauses_existing_config_task(self, db, monkeypatch, tmp_path):
        self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "daily-triage": JobConfig(
                    enabled=False,
                    schedule="0 8 * * *",
                    workspace="admin",
                    prompt="Run the daily triage memo.",
                )
            },
        )
        await create_task(
            ScheduledTask(
                id="job-daily-triage",
                group_folder="admin",
                chat_jid="slack:CADMIN",
                prompt="Run the daily triage memo.",
                schedule_type="cron",
                schedule_value="0 8 * * *",
                context_mode="isolated",
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        await reconcile_workspaces({}, [], AsyncMock())

        tasks = await get_all_tasks()
        assert tasks[0].status == "paused"
