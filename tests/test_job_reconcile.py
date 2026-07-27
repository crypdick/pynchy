"""Tests for config-backed job reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import init_test_database, make_settings

from pynchy.config.jobs import JobConfig
from pynchy.config.models import ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator.workspace_config import reconcile_workspaces
from pynchy.state import (
    create_task,
    get_active_task_for_group,
    get_all_tasks,
    get_task_run_logs,
    log_task_run,
)
from pynchy.types import ScheduledTask, SessionPolicy, TaskRunLog, WorkspaceProfile


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
        assert task.session_policy is SessionPolicy.RESET_BEFORE_RUN
        assert task.repo_access is None
        assert task.config_job_name == "daily-triage"
        assert task.config_job_is_deterministic is False
        assert task.config_job_command is None

    async def test_deterministic_job_persists_its_execution_values(self, db, monkeypatch, tmp_path):
        settings = self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "watchdog": JobConfig(
                    schedule="0 8 * * *",
                    workspace="admin",
                    agent=False,
                    command="scripts/watchdog.py",
                    cwd="runtime",
                    timeout_seconds=42,
                    display_name="Watchdog",
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

        task = (await get_all_tasks())[0]
        assert task.config_job_is_deterministic is True
        assert task.config_job_command == "scripts/watchdog.py"
        assert task.config_job_cwd == str((settings.project_root / "runtime").resolve())
        assert task.config_job_timeout_seconds == 42
        assert task.config_job_display_name == "Watchdog"

    async def test_workspace_job_records_only_its_config_provenance(
        self, db, monkeypatch, tmp_path
    ):
        settings = make_settings(
            groups_dir=tmp_path / "groups",
            profiles={"relationships": ProfileConfig()},
            workspaces={"relationships": WorkspaceConfig(profiles=["relationships"])},
            jobs={
                "fam_daily_checkin": JobConfig(
                    enabled=True,
                    schedule="0 8 * * *",
                    workspace="relationships",
                    prompt="Check in with the family.",
                )
            },
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.get_settings", lambda: settings
        )
        registered = {
            "discord:channel:relationships": WorkspaceProfile(
                jid="discord:channel:relationships",
                name="Relationships",
                folder="relationships",
                trigger="@Pynchy",
            )
        }

        await reconcile_workspaces(registered, [], AsyncMock())

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.group_folder == "relationships"
        assert task.chat_jid == "discord:channel:relationships"
        assert task.config_job_name == "fam_daily_checkin"

    async def test_agent_job_can_continue_its_durable_session(self, db, monkeypatch, tmp_path):
        self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "ongoing-research": JobConfig(
                    schedule="0 8 * * *",
                    workspace="admin",
                    prompt="Continue the research.",
                    reset_before_run=False,
                )
            },
        )
        registered = {
            "discord:channel:admin": WorkspaceProfile(
                jid="discord:channel:admin",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
            )
        }

        await reconcile_workspaces(registered, [], AsyncMock())

        tasks = await get_all_tasks()
        assert len(tasks) == 1
        assert tasks[0].session_policy is SessionPolicy.CONTINUE

    async def test_scoped_jobs_bind_policy_owner_to_its_physical_parent(
        self, db, monkeypatch, tmp_path
    ):
        settings = make_settings(
            groups_dir=tmp_path / "groups",
            profiles={
                "category": ProfileConfig(),
                "fam": ProfileConfig(repo="crypdick/fam"),
                "pynchy-dev": ProfileConfig(
                    repo="crypdick/pynchy",
                    execution_mode="host",
                    cwd="/srv/pynchy",
                    is_admin=True,
                ),
            },
            workspaces={
                "relationships": WorkspaceConfig(
                    profiles=["category"],
                    scopes=[{"workspace": "fam", "profiles": ["fam"]}],
                ),
                "admin": WorkspaceConfig(
                    profiles=["category"],
                    scopes=[{"workspace": "pynchy-dev", "profiles": ["pynchy-dev"]}],
                ),
            },
            jobs={
                "fam-check": JobConfig(
                    schedule="0 8 * * *",
                    workspace="fam",
                    prompt="Check fam.",
                ),
                "pynchy-check": JobConfig(
                    schedule="0 9 * * *",
                    workspace="pynchy-dev",
                    prompt="Check Pynchy.",
                ),
            },
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.get_settings", lambda: settings
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_placement.get_settings", lambda: settings
        )
        registered = {
            "discord:channel:relationships": WorkspaceProfile(
                jid="discord:channel:relationships",
                name="Relationships",
                folder="relationships",
                trigger="@Pynchy",
            ),
            "discord:channel:admin": WorkspaceProfile(
                jid="discord:channel:admin",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
            ),
        }

        await reconcile_workspaces(registered, [], AsyncMock())

        tasks = {task.group_folder: task for task in await get_all_tasks()}
        assert tasks["fam"].chat_jid == "discord:channel:relationships"
        assert tasks["fam"].derived_thread_name == "fam | fam-check"
        assert tasks["pynchy-dev"].chat_jid == "discord:channel:admin"
        assert tasks["pynchy-dev"].derived_thread_name == "pynchy-dev | pynchy-check"

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
        assert task.next_run is None

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
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
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
        assert task.config_job_name == "daily-triage"

    async def test_job_reconcile_rebinds_an_explicit_parent_workspace(
        self, db, monkeypatch, tmp_path
    ):
        settings = make_settings(
            groups_dir=tmp_path / "groups",
            profiles={"shared": ProfileConfig()},
            workspaces={
                "admin": WorkspaceConfig(profiles=["shared"]),
                "relationships": WorkspaceConfig(profiles=["shared"]),
            },
            jobs={
                "daily-triage": JobConfig(
                    enabled=True,
                    schedule="0 8 * * *",
                    workspace="relationships",
                    prompt="Run the daily triage memo.",
                )
            },
        )
        monkeypatch.setattr(
            "pynchy.host.orchestrator.workspace_config.get_settings",
            lambda: settings,
        )
        await create_task(
            ScheduledTask(
                id="job-daily-triage",
                group_folder="admin",
                chat_jid="slack:CADMIN",
                prompt="Run the daily triage memo.",
                schedule_type="cron",
                schedule_value="0 8 * * *",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                status="active",
                created_at=datetime.now(UTC).isoformat(),
                config_job_name="daily-triage",
            )
        )
        registered = {
            "discord:channel:relationships": WorkspaceProfile(
                jid="discord:channel:relationships",
                name="Relationships",
                folder="relationships",
                trigger="@Pynchy",
            )
        }

        await reconcile_workspaces(registered, [], AsyncMock())

        task = await get_active_task_for_group("relationships")
        assert task is not None
        assert task.group_folder == "relationships"
        assert task.chat_jid == "discord:channel:relationships"

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
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                status="active",
                created_at=datetime.now(UTC).isoformat(),
            )
        )

        await reconcile_workspaces({}, [], AsyncMock())

        tasks = await get_all_tasks()
        assert tasks[0].status == "paused"

    async def test_enabled_job_resets_failures_when_reactivating_config_task(
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
                chat_jid="slack:CADMIN",
                prompt="Run the daily triage memo.",
                schedule_type="cron",
                schedule_value="0 8 * * *",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                status="paused",
                created_at=datetime.now(UTC).isoformat(),
                config_job_name="daily-triage",
                derived_thread_name="admin | daily-triage",
            )
        )
        await log_task_run(
            TaskRunLog(
                task_id="job-daily-triage",
                run_at="2026-07-20T00:00:00Z",
                duration_ms=1,
                status="error",
                error="stale implementation failure",
            )
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
        logs = await get_task_run_logs("job-daily-triage")
        assert task is not None
        assert [log.status for log in logs] == ["resumed", "error"]
