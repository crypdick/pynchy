"""Tests for IPC authorization and task scheduling."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pynchy.host.container_manager.ipc.registry import dispatch
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    create_host_job,
    create_task,
    get_all_tasks,
    get_host_job_by_id,
    get_task_by_id,
    get_task_run_logs,
)
from pynchy.workspace.api import WorkspaceProfile
from tests.ipc_auth_support import (
    _test_settings,
)

pytest_plugins = ("tests.ipc_auth_support",)

ADMIN_GROUP = WorkspaceProfile(
    jid="admin-1@g.us",
    name="Admin",
    folder="admin-1",
    trigger="always",
    added_at="2024-01-01T00:00:00.000Z",
    is_admin=True,
)

OTHER_GROUP = WorkspaceProfile(
    jid="other@g.us",
    name="Other",
    folder="other-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

THIRD_GROUP = WorkspaceProfile(
    jid="third@g.us",
    name="Third",
    folder="third-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)


class TestPauseTaskAuth:
    @pytest.fixture(autouse=True)
    async def _create_tasks(self, deps):
        await create_task(
            ScheduledTask(
                id="task-admin",
                group_folder="admin-1",
                chat_jid="admin-1@g.us",
                prompt="admin task",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )
        await create_task(
            ScheduledTask(
                id="task-other",
                group_folder="other-group",
                chat_jid="other@g.us",
                prompt="other task",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

    async def test_admin_can_pause_any_task(self, deps):
        await dispatch({"type": "pause_task", "taskId": "task-other"}, "admin-1", True, deps)
        task = await get_task_by_id("task-other")
        assert task is not None
        assert task.status == "paused"

    async def test_non_admin_can_pause_own_task(self, deps):
        await dispatch(
            {"type": "pause_task", "taskId": "task-other"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-other")
        assert task is not None
        assert task.status == "paused"

    async def test_non_admin_cannot_pause_other_groups_task(self, deps):
        await dispatch(
            {"type": "pause_task", "taskId": "task-admin"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-admin")
        assert task is not None
        assert task.status == "active"


class TestResumeTaskAuth:
    @pytest.fixture(autouse=True)
    async def _create_tasks(self, deps):
        await create_task(
            ScheduledTask(
                id="task-paused",
                group_folder="other-group",
                chat_jid="other@g.us",
                prompt="paused task",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="paused",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

    async def test_admin_can_resume_any_task(self, deps):
        await dispatch({"type": "resume_task", "taskId": "task-paused"}, "admin-1", True, deps)
        task = await get_task_by_id("task-paused")
        logs = await get_task_run_logs("task-paused")
        assert task is not None
        assert task.status == "active"
        assert logs[0].status == "resumed"

    async def test_non_admin_can_resume_own_task(self, deps):
        await dispatch(
            {"type": "resume_task", "taskId": "task-paused"},
            "other-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-paused")
        assert task is not None
        assert task.status == "active"

    async def test_non_admin_cannot_resume_other_groups_task(self, deps):
        await dispatch(
            {"type": "resume_task", "taskId": "task-paused"},
            "third-group",
            False,
            deps,
        )
        task = await get_task_by_id("task-paused")
        assert task is not None
        assert task.status == "paused"


class TestCancelTaskAuth:
    async def test_admin_can_cancel_any_task(self, deps):
        await create_task(
            ScheduledTask(
                id="task-to-cancel",
                group_folder="other-group",
                chat_jid="other@g.us",
                prompt="cancel me",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run=None,
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await dispatch({"type": "cancel_task", "taskId": "task-to-cancel"}, "admin-1", True, deps)
        assert await get_task_by_id("task-to-cancel") is None

    async def test_non_admin_can_cancel_own_task(self, deps):
        await create_task(
            ScheduledTask(
                id="task-own",
                group_folder="other-group",
                chat_jid="other@g.us",
                prompt="my task",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run=None,
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await dispatch(
            {"type": "cancel_task", "taskId": "task-own"},
            "other-group",
            False,
            deps,
        )
        assert await get_task_by_id("task-own") is None

    async def test_non_admin_cannot_cancel_other_groups_task(self, deps):
        await create_task(
            ScheduledTask(
                id="task-foreign",
                group_folder="admin-1",
                chat_jid="admin-1@g.us",
                prompt="not yours",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run=None,
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await dispatch(
            {"type": "cancel_task", "taskId": "task-foreign"},
            "other-group",
            False,
            deps,
        )
        assert await get_task_by_id("task-foreign") is not None


class TestRegisterGroupAuth:
    async def test_non_admin_cannot_register_a_group(self, deps):
        await dispatch(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@pynchy",
            },
            "other-group",
            False,
            deps,
        )

        assert deps.workspaces().get("new@g.us") is None


class TestIpcMessageAuth:
    @staticmethod
    def is_message_authorized(
        source_group: str,
        is_admin: bool,  # tiny test helper mirrors IPC auth semantics.
        target_chat_jid: str,
        workspaces: dict[str, WorkspaceProfile],
    ) -> bool:
        target_group = workspaces.get(target_chat_jid)
        return is_admin or (target_group is not None and target_group.folder == source_group)

    def test_admin_can_send_to_any_group(self, deps):
        groups = deps.workspaces()
        assert self.is_message_authorized("admin-1", True, "other@g.us", groups)
        assert self.is_message_authorized("admin-1", True, "third@g.us", groups)

    def test_non_admin_can_send_to_own_chat(self, deps):
        groups = deps.workspaces()
        assert self.is_message_authorized("other-group", False, "other@g.us", groups)

    def test_non_admin_cannot_send_to_other_chat(self, deps):
        groups = deps.workspaces()
        assert not self.is_message_authorized("other-group", False, "admin-1@g.us", groups)
        assert not self.is_message_authorized("other-group", False, "third@g.us", groups)

    def test_non_admin_cannot_send_to_unregistered(self, deps):
        groups = deps.workspaces()
        assert not self.is_message_authorized("other-group", False, "unknown@g.us", groups)

    def test_admin_can_send_to_unregistered(self, deps):
        groups = deps.workspaces()
        assert self.is_message_authorized("admin-1", True, "unknown@g.us", groups)


@pytest.mark.action("workspace.group.register")
class TestRegisterGroupSuccess:
    async def test_admin_can_register_new_group(self, deps):
        await dispatch(
            {
                "type": "register_group",
                "jid": "new@g.us",
                "name": "New Group",
                "folder": "new-group",
                "trigger": "@pynchy",
            },
            "admin-1",
            True,
            deps,
        )

        group = deps.workspaces().get("new@g.us")
        assert group is not None
        assert group.name == "New Group"
        assert group.folder == "new-group"
        assert group.trigger == "@pynchy"

    async def test_rejects_missing_fields(self, deps):
        await dispatch(
            {
                "type": "register_group",
                "jid": "partial@g.us",
                "name": "Partial",
                # missing folder and trigger
            },
            "admin-1",
            True,
            deps,
        )

        assert deps.workspaces().get("partial@g.us") is None


class TestAuthorizedTaskActionEdges:
    """Edge cases for _authorized_task_action used by pause/resume/cancel."""

    async def test_missing_task_id_is_noop(self, deps):
        """When taskId is missing from the data, nothing happens."""
        # Create a task to verify it stays untouched
        await create_task(
            ScheduledTask(
                id="untouched",
                group_folder="other-group",
                chat_jid="other@g.us",
                prompt="should not change",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        # No taskId in data — should silently return
        await dispatch({"type": "pause_task"}, "admin-1", True, deps)

        task = await get_task_by_id("untouched")
        assert task is not None
        assert task.status == "active"

    async def test_nonexistent_task_id_logs_warning(self, deps):
        """Pausing a task that doesn't exist should not crash."""
        await dispatch(
            {"type": "pause_task", "taskId": "does-not-exist"},
            "admin-1",
            True,
            deps,
        )

    async def test_cancel_nonexistent_task_is_safe(self, deps):
        """Cancelling a task that doesn't exist should not crash."""
        await dispatch(
            {"type": "cancel_task", "taskId": "ghost-task"},
            "other-group",
            False,
            deps,
        )

    async def test_unknown_ipc_type_is_ignored(self, deps):
        """Unrecognized IPC types should not crash."""
        await dispatch(
            {"type": "totally_made_up"},
            "admin-1",
            True,
            deps,
        )


@pytest.mark.action("deployment.apply")
class TestDeployAuth:
    """Deploy IPC is admin-only. Non-admin attempts should be silently blocked."""

    async def test_non_admin_cannot_deploy(self, deps):
        await dispatch(
            {
                "type": "deploy",
                "rebuildContainer": False,
                "resumePrompt": "test",
                "headSha": "abc123",
                "chatJid": "other@g.us",
            },
            "other-group",
            False,
            deps,
        )
        assert not deps.requested_deploys

    async def test_admin_deploy_starts_temporal_workflow(self, deps):
        """An admin deploy delegates a fully parsed request to composition."""
        await dispatch(
            {
                "type": "deploy",
                "rebuildContainer": False,
                "resumePrompt": "Deploy complete.",
                "headSha": "abc123",
                "sessionId": "sess-1",
                "chatJid": "admin-1@g.us",
            },
            "admin-1",
            True,
            deps,
        )
        assert deps.requested_deploys == [("admin-1@g.us", "abc123", False, "Deploy complete.")]


@pytest.mark.action("lifecycle.context.reset")
class TestResetContextExecution:
    """Tests for the reset_context IPC command execution paths."""

    async def test_non_admin_cannot_reset_another_workspace(self, deps, tmp_path):
        with patch(
            "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
            return_value=_test_settings(data_dir=tmp_path / "data"),
        ):
            await dispatch(
                {
                    "type": "reset_context",
                    "chatJid": "admin-1@g.us",
                    "message": "replace admin context",
                    "groupFolder": "admin-1",
                },
                "other-group",
                False,
                deps,
            )

        assert not deps.cleared_sessions
        assert not deps.cleared_chats
        assert not (tmp_path / "data" / "ipc" / "admin-1" / "reset_prompt.json").exists()

    async def test_reset_context_clears_session_and_chat(self, deps, tmp_path):
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
        ):
            (tmp_path / "data" / "ipc" / "admin-1").mkdir(parents=True)
            await dispatch(
                {
                    "type": "reset_context",
                    "chatJid": "admin-1@g.us",
                    "message": "Start fresh",
                    "groupFolder": "admin-1",
                },
                "admin-1",
                True,
                deps,
            )

            assert "admin-1" in deps.cleared_sessions
            assert "admin-1@g.us" in deps.cleared_chats
            assert "admin-1@g.us" in deps.enqueued_checks

    async def test_reset_context_writes_reset_prompt_file(self, deps, tmp_path):
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
        ):
            (tmp_path / "data" / "ipc" / "admin-1").mkdir(parents=True)
            await dispatch(
                {
                    "type": "reset_context",
                    "chatJid": "admin-1@g.us",
                    "message": "Start fresh",
                    "groupFolder": "admin-1",
                },
                "admin-1",
                True,
                deps,
            )

            reset_file = tmp_path / "data" / "ipc" / "admin-1" / "reset_prompt.json"
            assert reset_file.exists()
            data = json.loads(reset_file.read_text())
            assert data["message"] == "Start fresh"
            assert data["chatJid"] == "admin-1@g.us"
            assert data["needsDirtyRepoCheck"] is True

    async def test_reset_context_rejects_missing_chat_jid(self, deps):
        """reset_context without chatJid should bail without clearing."""
        await dispatch(
            {
                "type": "reset_context",
                "message": "Start fresh",
                "groupFolder": "admin-1",
            },
            "admin-1",
            True,
            deps,
        )

        assert len(deps.cleared_sessions) == 0
        assert len(deps.cleared_chats) == 0

    async def test_reset_context_without_message_still_clears(self, deps, tmp_path):
        """reset_context without message should clear session but skip handoff file."""
        with (
            patch(
                "pynchy.host.container_manager.ipc.handlers_lifecycle.get_settings",
                return_value=_test_settings(data_dir=tmp_path / "data"),
            ),
        ):
            (tmp_path / "data" / "ipc" / "admin-1").mkdir(parents=True)
            await dispatch(
                {
                    "type": "reset_context",
                    "chatJid": "admin-1@g.us",
                    "groupFolder": "admin-1",
                },
                "admin-1",
                True,
                deps,
            )

            assert "admin-1" in deps.cleared_sessions
            assert "admin-1@g.us" in deps.cleared_chats
            reset_file = tmp_path / "data" / "ipc" / "admin-1" / "reset_prompt.json"
            assert not reset_file.exists()


class TestCreatePeriodicAgentAuth:
    """Tests for create_periodic_agent authorization and validation."""

    async def test_non_admin_cannot_create_periodic_agent(self, deps):
        await dispatch(
            {
                "type": "create_periodic_agent",
                "name": "my-agent",
                "schedule": "0 9 * * *",
                "prompt": "do something",
            },
            "other-group",
            False,
            deps,
        )

        # No tasks should be created
        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_rejects_missing_required_fields(self, deps):
        """create_periodic_agent without name/schedule/prompt should bail."""
        await dispatch(
            {
                "type": "create_periodic_agent",
                "name": "my-agent",
                # missing schedule and prompt
            },
            "admin-1",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 0

    async def test_rejects_invalid_cron_expression(self, deps):
        """create_periodic_agent with invalid cron should bail."""
        await dispatch(
            {
                "type": "create_periodic_agent",
                "name": "bad-cron-agent",
                "schedule": "not valid cron",
                "prompt": "do something",
            },
            "admin-1",
            True,
            deps,
        )

        tasks = await get_all_tasks()
        assert len(tasks) == 0


class TestHostJobPauseAuth:
    """Tests for pause_task routing host job IDs to update_host_job."""

    @pytest.fixture(autouse=True)
    async def _create_host_job(self, deps):
        await create_host_job(
            {
                "id": "host-job-1",
                "name": "test-host-job",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "admin-1",
                "enabled": True,
            }
        )

    async def test_admin_can_pause_host_job(self, deps):
        await dispatch({"type": "pause_task", "taskId": "host-job-1"}, "admin-1", True, deps)
        job = await get_host_job_by_id("host-job-1")
        assert job is not None
        assert job.status == "paused"

    async def test_non_admin_cannot_pause_host_job(self, deps):
        await dispatch(
            {"type": "pause_task", "taskId": "host-job-1"},
            "other-group",
            False,
            deps,
        )
        job = await get_host_job_by_id("host-job-1")
        assert job is not None
        assert job.status == "active"


class TestHostJobResumeAuth:
    """Tests for resume_task routing host job IDs to update_host_job."""

    @pytest.fixture(autouse=True)
    async def _create_host_job(self, deps):
        await create_host_job(
            {
                "id": "host-paused-1",
                "name": "paused-host-job",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "paused",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "admin-1",
                "enabled": True,
            }
        )

    async def test_admin_can_resume_host_job(self, deps):
        await dispatch({"type": "resume_task", "taskId": "host-paused-1"}, "admin-1", True, deps)
        job = await get_host_job_by_id("host-paused-1")
        assert job is not None
        assert job.status == "active"

    async def test_non_admin_cannot_resume_host_job(self, deps):
        await dispatch(
            {"type": "resume_task", "taskId": "host-paused-1"},
            "other-group",
            False,
            deps,
        )
        job = await get_host_job_by_id("host-paused-1")
        assert job is not None
        assert job.status == "paused"
