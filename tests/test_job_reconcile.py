"""Tests for config-backed job reconciliation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import configure_workspace_placement_for, init_test_database, make_settings

from pynchy.config.api import JobConfig, ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator import config_jobs
from pynchy.host.orchestrator.config_jobs import reconcile_agent_jobs
from pynchy.host.orchestrator.workspace_config import reconcile_workspaces
from pynchy.host.orchestrator.workspace_placement import WorkspacePlacement
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
    TaskRunLog,
)
from pynchy.state import (
    create_task,
    get_active_task_for_group,
    get_all_tasks,
    get_task_run_logs,
    log_task_run,
    record_task_completion,
)
from pynchy.workspace.api import WorkspaceProfile


class TestJobReconcile:
    @pytest.fixture
    async def db(self):
        await init_test_database()

    @pytest.fixture
    def registered(self):
        return {
            "slack:CADMIN": WorkspaceProfile(
                jid="slack:CADMIN",
                name="Admin",
                folder="admin",
                trigger="@Pynchy",
                is_admin=True,
            )
        }

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

    async def test_agent_job_creates_scheduled_task_for_workspace(
        self, db, monkeypatch, tmp_path, registered
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
                    memory=False,
                )
            },
        )

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
        assert task.memory_enabled is False
        assert task.repo_access is None
        assert task.config_job_name == "daily-triage"
        assert task.config_job_is_deterministic is False
        assert task.config_job_command is None

    @pytest.mark.parametrize("cwd", [None, "runtime"])
    async def test_deterministic_job_persists_its_execution_values(
        self, db, monkeypatch, tmp_path, registered, cwd
    ):
        settings = self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "watchdog": JobConfig(
                    interval_minutes=15,
                    workspace="admin",
                    agent=False,
                    command="scripts/watchdog.py",
                    cwd=cwd,
                    timeout_seconds=42,
                    display_name="Watchdog",
                )
            },
        )

        await reconcile_workspaces(registered, [], AsyncMock())

        task = (await get_all_tasks())[0]
        assert task.schedule_type == "interval"
        assert task.schedule_value == "900000"
        assert task.config_job_is_deterministic is True
        assert task.config_job_command == "scripts/watchdog.py"
        expected_cwd = (
            settings.project_root if cwd is None else (settings.project_root / cwd).resolve()
        )
        assert task.config_job_cwd == str(expected_cwd)
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
        configure_workspace_placement_for(settings)
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
        assert tasks["fam"].derived_thread_name == "⚙️ fam-check"
        assert tasks["pynchy-dev"].chat_jid == "discord:channel:admin"
        assert tasks["pynchy-dev"].derived_thread_name == "⚙️ pynchy-check"

    async def test_one_time_agent_job_creates_once_task(
        self, db, monkeypatch, tmp_path, registered
    ):
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

        await reconcile_workspaces(registered, [], AsyncMock())

        task = await get_active_task_for_group("admin")
        assert task is not None
        assert task.id == "job-cancel-youtube-premium"
        assert task.schedule_type == "once"
        assert task.schedule_value == run_at
        assert task.next_run is None

    async def test_job_reconcile_updates_config_and_preserves_execution_state(
        self, db, monkeypatch, tmp_path
    ):
        settings = self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "daily-triage": JobConfig(
                    enabled=True,
                    schedule="0 8 * * *",
                    workspace="admin",
                    prompt="Run the daily triage memo.",
                    memory=False,
                    pre_run_command="scripts/prepare.py",
                    pre_run_cwd="/srv/triage",
                    pre_run_timeout_seconds=30,
                )
            },
        )
        await create_task(
            ScheduledTask(
                id="job-daily-triage",
                group_folder="admin",
                chat_jid="slack:COLD",
                prompt="Old triage prompt.",
                schedule_type="interval",
                schedule_value="900000",
                session_policy=SessionPolicy.CONTINUE,
                status="active",
                created_at="2026-07-08T08:00:00+00:00",
                repo_access="crypdick/pynchy",
                config_job_is_deterministic=True,
                config_job_command="scripts/old.py",
                config_job_cwd="/srv/old",
                config_job_timeout_seconds=42,
                config_job_display_name="Old triage",
                bound_chat_jid="slack:CTRIAGE",
                bound_group_folder="admin/daily-triage",
                conversation_id="triage-conversation",
                last_reset_occurrence="2026-07-08T08:00:00+00:00",
                occurrence_generation=3,
                occurrence_due_at="2026-07-08T08:30:00+00:00",
                superseded_occurrence_generation=2,
                superseded_occurrence_due_at="2026-07-08T08:15:00+00:00",
            )
        )
        await record_task_completion(
            "job-daily-triage", last_result="Previous triage memo", completed=False
        )
        await log_task_run(
            TaskRunLog(
                task_id="job-daily-triage",
                run_at="2026-07-08T08:00:00+00:00",
                duration_ms=100,
                status="success",
                result="Previous triage memo",
            )
        )
        original = (await get_all_tasks())[0]
        history = await get_task_run_logs(original.id)
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

        expected = replace(
            original,
            chat_jid="slack:CNEW",
            prompt="Run the daily triage memo.",
            schedule_type="cron",
            schedule_value="0 8 * * *",
            session_policy=SessionPolicy.RESET_BEFORE_RUN,
            memory_enabled=False,
            repo_access=None,
            config_job_name="daily-triage",
            config_job_is_deterministic=False,
            config_job_command=None,
            config_job_cwd=None,
            config_job_timeout_seconds=None,
            config_job_display_name=None,
            config_job_pre_run_command="scripts/prepare.py",
            config_job_pre_run_cwd="/srv/triage",
            config_job_pre_run_timeout_seconds=30,
            derived_thread_name="⚙️ daily-triage",
        )
        assert await get_all_tasks() == [expected]

        settings.jobs["daily-triage"] = JobConfig(
            interval_minutes=5,
            workspace="admin",
            agent=False,
            command="scripts/triage.py",
            cwd="/srv/triage",
            timeout_seconds=60,
            display_name="Triage",
        )
        await reconcile_workspaces(registered, [], AsyncMock())

        assert await get_all_tasks() == [
            replace(
                expected,
                prompt="",
                schedule_type="interval",
                schedule_value="300000",
                memory_enabled=True,
                config_job_is_deterministic=True,
                config_job_command="scripts/triage.py",
                config_job_cwd="/srv/triage",
                config_job_timeout_seconds=60,
                config_job_display_name="Triage",
                config_job_pre_run_command=None,
                config_job_pre_run_cwd=None,
                config_job_pre_run_timeout_seconds=None,
                derived_thread_name="⚙️ Triage",
            )
        ]
        assert await get_task_run_logs(original.id) == history

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

    async def test_disabled_job_without_existing_task_is_ignored(self, db, monkeypatch, tmp_path):
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

        await reconcile_workspaces({}, [], AsyncMock())

        assert await get_all_tasks() == []

    async def test_reconciling_an_unchanged_agent_job_has_no_updates(
        self, db, monkeypatch, tmp_path, registered
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

        await reconcile_workspaces(registered, [], AsyncMock())
        original = await get_all_tasks()
        await reconcile_workspaces(registered, [], AsyncMock())

        assert await get_all_tasks() == original

    async def test_enabled_job_resets_failures_when_reactivating_config_task(
        self, db, monkeypatch, tmp_path, registered
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

        await reconcile_workspaces(registered, [], AsyncMock())

        task = await get_active_task_for_group("admin")
        logs = await get_task_run_logs("job-daily-triage")
        assert task is not None
        assert [log.status for log in logs] == ["resumed", "error"]

    async def test_reconcile_skips_host_and_unavailable_workspace_jobs(
        self, monkeypatch, tmp_path
    ) -> None:
        settings = self._patch_settings(monkeypatch, tmp_path, jobs={})
        settings.jobs["host-check"] = JobConfig(
            workspace="host",
            schedule="0 8 * * *",
            command="scripts/check.py",
        )
        settings.jobs["orphaned"] = JobConfig(
            schedule="0 9 * * *",
            workspace="missing",
            prompt="Check the missing workspace.",
        )

        assert await reconcile_agent_jobs({}, settings, lambda _folder: None) == set()

    async def test_reconcile_skips_job_without_resolved_workspace_config(
        self, monkeypatch, tmp_path
    ) -> None:
        settings = self._patch_settings(
            monkeypatch,
            tmp_path,
            jobs={
                "unresolved": JobConfig(
                    schedule="0 8 * * *",
                    workspace="admin",
                    prompt="Check the unresolved workspace.",
                )
            },
        )
        group = WorkspaceProfile(
            jid="slack:CADMIN",
            name="Admin",
            folder="admin",
            trigger="@Pynchy",
            is_admin=True,
        )
        monkeypatch.setattr(
            config_jobs,
            "resolve_workspace_placement",
            lambda _workspaces, _folder: WorkspacePlacement(
                owner=group,
                control_parent=group,
            ),
        )

        assert (
            await reconcile_agent_jobs({group.jid: group}, settings, lambda _folder: None) == set()
        )
