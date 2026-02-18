"""Tests for data models in types.py.

Tests serialization, deserialization, default values, and validation for
the core dataclasses used across the codebase. These models define the
contract between host and container — wrong defaults or missing fields
cause silent failures.
"""

from __future__ import annotations

from pynchy.types import (
    AdditionalMount,
    AllowedRoot,
    ContainerConfig,
    ContainerInput,
    ContainerOutput,
    MountAllowlist,
    NewMessage,
    ScheduledTask,
    TaskRunLog,
    VolumeMount,
)

# ---------------------------------------------------------------------------
# ContainerConfig
# ---------------------------------------------------------------------------


class TestContainerConfig:
    """Test ContainerConfig.from_dict deserialization.

    ContainerConfig is parsed from workspace YAML — from_dict must handle
    missing keys, empty lists, and nested AdditionalMount objects.
    """

    def test_from_dict_empty(self):
        config = ContainerConfig.from_dict({})
        assert config.additional_mounts == []
        assert config.timeout is None

    def test_from_dict_with_timeout(self):
        config = ContainerConfig.from_dict({"timeout": 600.0})
        assert config.timeout == 600.0

    def test_from_dict_with_mounts(self):
        config = ContainerConfig.from_dict(
            {
                "additional_mounts": [
                    {"host_path": "/data/models", "container_path": "/models", "readonly": True},
                    {"host_path": "/tmp/cache"},
                ]
            }
        )
        assert len(config.additional_mounts) == 2
        assert config.additional_mounts[0].host_path == "/data/models"
        assert config.additional_mounts[0].container_path == "/models"
        assert config.additional_mounts[0].readonly is True
        # Second mount uses defaults
        assert config.additional_mounts[1].host_path == "/tmp/cache"
        assert config.additional_mounts[1].container_path is None
        assert config.additional_mounts[1].readonly is True

    def test_from_dict_with_all_fields(self):
        config = ContainerConfig.from_dict(
            {
                "additional_mounts": [
                    {"host_path": "~/docs", "container_path": "/docs", "readonly": False}
                ],
                "timeout": 300.0,
            }
        )
        assert config.timeout == 300.0
        assert len(config.additional_mounts) == 1
        assert config.additional_mounts[0].readonly is False

    def test_from_dict_ignores_unknown_keys(self):
        """Unknown keys in the dict are silently ignored."""
        config = ContainerConfig.from_dict(
            {"timeout": 120, "unknown_key": "value", "additional_mounts": []}
        )
        assert config.timeout == 120


# ---------------------------------------------------------------------------
# AdditionalMount
# ---------------------------------------------------------------------------


class TestAdditionalMount:
    """Test AdditionalMount default values."""

    def test_defaults(self):
        mount = AdditionalMount(host_path="/data")
        assert mount.host_path == "/data"
        assert mount.container_path is None
        assert mount.readonly is True

    def test_writable_mount(self):
        mount = AdditionalMount(host_path="/tmp", container_path="/workspace/tmp", readonly=False)
        assert mount.readonly is False
        assert mount.container_path == "/workspace/tmp"


# ---------------------------------------------------------------------------
# AllowedRoot & MountAllowlist
# ---------------------------------------------------------------------------


class TestAllowedRoot:
    def test_defaults(self):
        root = AllowedRoot(path="/data")
        assert root.allow_read_write is False
        assert root.description is None

    def test_with_all_fields(self):
        root = AllowedRoot(path="~/projects", allow_read_write=True, description="User projects")
        assert root.allow_read_write is True
        assert root.description == "User projects"


class TestMountAllowlist:
    def test_defaults(self):
        allowlist = MountAllowlist()
        assert allowlist.allowed_roots == []
        assert allowlist.blocked_patterns == []
        assert allowlist.non_admin_read_only is True

    def test_with_entries(self):
        allowlist = MountAllowlist(
            allowed_roots=[AllowedRoot(path="/data")],
            blocked_patterns=["*.secret"],
            non_admin_read_only=False,
        )
        assert len(allowlist.allowed_roots) == 1
        assert len(allowlist.blocked_patterns) == 1
        assert allowlist.non_admin_read_only is False


# ---------------------------------------------------------------------------
# ScheduledTask
# ---------------------------------------------------------------------------


class TestScheduledTask:
    """Test ScheduledTask.to_snapshot_dict serialization.

    The snapshot dict is written to IPC for containers to read — wrong
    field names or missing fields break task listing in the agent.
    """

    def test_to_snapshot_dict_all_fields(self):
        task = ScheduledTask(
            id="task-1",
            group_folder="my-group",
            chat_jid="group@g.us",
            prompt="Check for updates",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            context_mode="isolated",
            next_run="2024-02-01T09:00:00Z",
            status="active",
        )
        snapshot = task.to_snapshot_dict()
        assert snapshot == {
            "id": "task-1",
            "type": "agent",
            "groupFolder": "my-group",
            "prompt": "Check for updates",
            "schedule_type": "cron",
            "schedule_value": "0 9 * * *",
            "status": "active",
            "next_run": "2024-02-01T09:00:00Z",
        }

    def test_to_snapshot_dict_excludes_internal_fields(self):
        """Snapshot should not include chat_jid, last_run, last_result, etc."""
        task = ScheduledTask(
            id="task-2",
            group_folder="other",
            chat_jid="other@g.us",
            prompt="Clean up",
            schedule_type="interval",
            schedule_value="3600000",
            context_mode="group",
            last_run="2024-01-31T08:00:00Z",
            last_result="OK",
        )
        snapshot = task.to_snapshot_dict()
        assert "chat_jid" not in snapshot
        assert "last_run" not in snapshot
        assert "last_result" not in snapshot
        assert "context_mode" not in snapshot

    def test_to_snapshot_dict_null_next_run(self):
        """next_run can be None for newly created tasks."""
        task = ScheduledTask(
            id="task-3",
            group_folder="g",
            chat_jid="g@g.us",
            prompt="do stuff",
            schedule_type="once",
            schedule_value="2024-03-01T12:00:00",
            context_mode="isolated",
        )
        snapshot = task.to_snapshot_dict()
        assert snapshot["next_run"] is None

    def test_defaults(self):
        """Verify default field values."""
        task = ScheduledTask(
            id="t",
            group_folder="g",
            chat_jid="j",
            prompt="p",
            schedule_type="cron",
            schedule_value="* * * * *",
            context_mode="group",
        )
        assert task.next_run is None
        assert task.last_run is None
        assert task.last_result is None
        assert task.status == "active"
        assert task.created_at == ""
        assert task.pynchy_repo_access is False


# ---------------------------------------------------------------------------
# NewMessage
# ---------------------------------------------------------------------------


class TestNewMessage:
    """Test NewMessage default field values."""

    def test_defaults(self):
        msg = NewMessage(
            id="m1",
            chat_jid="group@g.us",
            sender="user@s.whatsapp.net",
            sender_name="Alice",
            content="Hello",
            timestamp="2024-01-01T00:00:00Z",
        )
        assert msg.is_from_me is None
        assert msg.message_type == "user"
        assert msg.metadata is None

    def test_all_fields(self):
        msg = NewMessage(
            id="m2",
            chat_jid="group@g.us",
            sender="bot",
            sender_name="Pynchy",
            content="Response",
            timestamp="2024-01-01T00:00:01Z",
            is_from_me=True,
            message_type="assistant",
            metadata={"model": "claude-3"},
        )
        assert msg.is_from_me is True
        assert msg.message_type == "assistant"
        assert msg.metadata == {"model": "claude-3"}

    def test_system_message_type(self):
        msg = NewMessage(
            id="m3",
            chat_jid="group@g.us",
            sender="system",
            sender_name="system",
            content="Session cleared",
            timestamp="2024-01-01T00:00:02Z",
            message_type="system",
        )
        assert msg.message_type == "system"


# ---------------------------------------------------------------------------
# ContainerInput
# ---------------------------------------------------------------------------


class TestContainerInput:
    """Test ContainerInput default field values."""

    def test_defaults(self):
        inp = ContainerInput(
            messages=[{"content": "hi"}],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=False,
        )
        assert inp.session_id is None
        assert inp.is_scheduled_task is False
        assert inp.system_notices is None
        assert inp.pynchy_repo_access is False
        assert inp.agent_core_module == "agent_runner.cores.claude"
        assert inp.agent_core_class == "ClaudeAgentCore"
        assert inp.agent_core_config is None


# ---------------------------------------------------------------------------
# ContainerOutput
# ---------------------------------------------------------------------------


class TestContainerOutput:
    """Test ContainerOutput default field values."""

    def test_defaults(self):
        out = ContainerOutput(status="success")
        assert out.result is None
        assert out.new_session_id is None
        assert out.error is None
        assert out.type == "result"
        assert out.thinking is None
        assert out.tool_name is None
        assert out.tool_input is None
        assert out.text is None
        assert out.system_subtype is None
        assert out.system_data is None
        assert out.tool_result_id is None
        assert out.tool_result_content is None
        assert out.tool_result_is_error is None
        assert out.result_metadata is None

    def test_error_output(self):
        out = ContainerOutput(status="error", error="something went wrong")
        assert out.status == "error"
        assert out.error == "something went wrong"

    def test_tool_use_output(self):
        out = ContainerOutput(
            status="success",
            type="tool_use",
            tool_name="Bash",
            tool_input={"command": "ls"},
        )
        assert out.type == "tool_use"
        assert out.tool_name == "Bash"
        assert out.tool_input == {"command": "ls"}


# ---------------------------------------------------------------------------
# VolumeMount
# ---------------------------------------------------------------------------


class TestVolumeMount:
    def test_defaults(self):
        mount = VolumeMount(host_path="/data", container_path="/data")
        assert mount.readonly is False

    def test_readonly_mount(self):
        mount = VolumeMount(host_path="/src", container_path="/app/src", readonly=True)
        assert mount.readonly is True


# ---------------------------------------------------------------------------
# TaskRunLog
# ---------------------------------------------------------------------------


class TestTaskRunLog:
    def test_success_log(self):
        log = TaskRunLog(
            task_id="t1",
            run_at="2024-01-01T00:00:00Z",
            duration_ms=1500.0,
            status="success",
            result="All good",
        )
        assert log.status == "success"
        assert log.result == "All good"
        assert log.error is None

    def test_error_log(self):
        log = TaskRunLog(
            task_id="t1",
            run_at="2024-01-01T00:00:00Z",
            duration_ms=500.0,
            status="error",
            error="Connection timed out",
        )
        assert log.status == "error"
        assert log.error == "Connection timed out"
        assert log.result is None
