"""Tests for container/agent_runner/src/agent_runner/main.py.

Tests core functions: build_sdk_messages, event_to_output, ContainerOutput,
ContainerInput, should_close, drain_ipc_input, build_core_config.
"""

from __future__ import annotations

import json

# We need to adjust the import path since agent_runner lives in container/
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "container" / "agent_runner" / "src"))

from agent_runner.core import AgentEvent
from agent_runner.ipc import drain_ipc_input, should_close
from agent_runner.main import (
    build_core_config,
    build_sdk_messages,
    event_to_output,
)
from agent_runner.models import ContainerInput, ContainerOutput

# ---------------------------------------------------------------------------
# ContainerOutput.to_dict
# ---------------------------------------------------------------------------


class TestContainerOutputToDict:
    """Test ContainerOutput serialization for each event type."""

    def test_result_basic(self):
        out = ContainerOutput(status="success", result="hello world")
        d = out.to_dict()
        assert d["type"] == "result"
        assert d["status"] == "success"
        assert d["result"] == "hello world"

    def test_result_with_session_id(self):
        out = ContainerOutput(status="success", result="done", new_session_id="sess-123")
        d = out.to_dict()
        assert d["new_session_id"] == "sess-123"

    def test_result_with_error(self):
        out = ContainerOutput(status="error", error="something broke")
        d = out.to_dict()
        assert d["error"] == "something broke"
        assert d["status"] == "error"

    def test_result_with_metadata(self):
        meta = {"total_cost_usd": 0.05, "duration_ms": 1200}
        out = ContainerOutput(status="success", result="ok", result_metadata=meta)
        d = out.to_dict()
        assert d["result_metadata"] == meta

    def test_result_omits_none_fields(self):
        out = ContainerOutput(status="success", result=None)
        d = out.to_dict()
        assert "new_session_id" not in d
        assert "error" not in d
        assert "result_metadata" not in d

    def test_thinking_type(self):
        out = ContainerOutput(status="success", type="thinking", thinking="let me think...")
        d = out.to_dict()
        assert d["type"] == "thinking"
        assert d["thinking"] == "let me think..."
        assert "result" not in d

    def test_tool_use_type(self):
        out = ContainerOutput(
            status="success",
            type="tool_use",
            tool_name="bash",
            tool_input={"command": "ls"},
        )
        d = out.to_dict()
        assert d["type"] == "tool_use"
        assert d["tool_name"] == "bash"
        assert d["tool_input"] == {"command": "ls"}

    def test_text_type(self):
        out = ContainerOutput(status="success", type="text", text="streaming text")
        d = out.to_dict()
        assert d["type"] == "text"
        assert d["text"] == "streaming text"

    def test_system_type(self):
        out = ContainerOutput(
            status="success",
            type="system",
            system_subtype="init",
            system_data={"session_id": "abc"},
        )
        d = out.to_dict()
        assert d["type"] == "system"
        assert d["system_subtype"] == "init"
        assert d["system_data"] == {"session_id": "abc"}

    def test_tool_result_type(self):
        out = ContainerOutput(
            status="success",
            type="tool_result",
            tool_result_id="tr-1",
            tool_result_content="file created",
            tool_result_is_error=False,
        )
        d = out.to_dict()
        assert d["type"] == "tool_result"
        assert d["tool_result_id"] == "tr-1"
        assert d["tool_result_content"] == "file created"
        assert d["tool_result_is_error"] is False


# ---------------------------------------------------------------------------
# ContainerInput
# ---------------------------------------------------------------------------


class TestContainerInput:
    """Test ContainerInput parsing from dict."""

    def test_minimal_input(self):
        data = {
            "messages": [{"content": "hi"}],
            "group_folder": "test",
            "chat_jid": "123@g.us",
            "is_admin": True,
        }
        ci = ContainerInput.from_dict(data)
        assert ci.messages == [{"content": "hi"}]
        assert ci.group_folder == "test"
        assert ci.chat_jid == "123@g.us"
        assert ci.is_admin is True
        assert ci.session_id is None
        assert ci.is_scheduled_task is False
        assert ci.repo_access is None

    def test_full_input(self):
        data = {
            "messages": [],
            "session_id": "sess-1",
            "group_folder": "grp",
            "chat_jid": "456@g.us",
            "is_admin": False,
            "is_scheduled_task": True,
            "system_notices": ["notice1"],
            "repo_access": "owner/pynchy",
            "agent_core_module": "custom.mod",
            "agent_core_class": "CustomCore",
            "agent_core_config": {"model": "gpt-4"},
        }
        ci = ContainerInput.from_dict(data)
        assert ci.session_id == "sess-1"
        assert ci.is_scheduled_task is True
        assert ci.system_notices == ["notice1"]
        assert ci.repo_access == "owner/pynchy"
        assert ci.agent_core_module == "custom.mod"
        assert ci.agent_core_class == "CustomCore"
        assert ci.agent_core_config == {"model": "gpt-4"}

    def test_defaults_agent_core(self):
        data = {
            "messages": [],
            "group_folder": "g",
            "chat_jid": "j",
            "is_admin": False,
        }
        ci = ContainerInput.from_dict(data)
        assert ci.agent_core_module == "agent_runner.cores.claude"
        assert ci.agent_core_class == "ClaudeAgentCore"

    def test_missing_required_field_raises(self):
        with pytest.raises(TypeError):
            ContainerInput.from_dict({"messages": []})  # missing group_folder, chat_jid, is_admin


# ---------------------------------------------------------------------------
# build_sdk_messages
# ---------------------------------------------------------------------------


class TestBuildSdkMessages:
    """Test message list to XML conversion."""

    def test_empty_list(self):
        assert build_sdk_messages([]) == ""

    def test_single_message(self):
        msgs = [
            {
                "sender_name": "Alice",
                "timestamp": "2024-01-01T00:00:00Z",
                "content": "Hello",
            }
        ]
        result = build_sdk_messages(msgs)
        assert "<messages>" in result
        assert "</messages>" in result
        assert 'sender="Alice"' in result
        assert ">Hello</message>" in result

    def test_multiple_messages(self):
        msgs = [
            {"sender_name": "Alice", "timestamp": "t1", "content": "Hi"},
            {"sender_name": "Bob", "timestamp": "t2", "content": "Hey"},
        ]
        result = build_sdk_messages(msgs)
        assert result.count("<message ") == 2
        assert 'sender="Alice"' in result
        assert 'sender="Bob"' in result

    def test_xml_escaping(self):
        msgs = [
            {
                "sender_name": 'Test "User"',
                "timestamp": "t",
                "content": "Use <b>bold</b> & stuff",
            }
        ]
        result = build_sdk_messages(msgs)
        assert "&amp;" in result
        assert "&lt;b&gt;" in result
        assert "&quot;" in result

    def test_missing_fields_use_defaults(self):
        msgs = [{}]
        result = build_sdk_messages(msgs)
        assert 'sender="Unknown"' in result

    def test_ampersand_in_content(self):
        msgs = [{"content": "A & B", "sender_name": "X", "timestamp": "t"}]
        result = build_sdk_messages(msgs)
        assert "A &amp; B" in result


# ---------------------------------------------------------------------------
# event_to_output
# ---------------------------------------------------------------------------


class TestEventToOutput:
    """Test AgentEvent to ContainerOutput conversion."""

    def test_thinking_event(self):
        event = AgentEvent(type="thinking", data={"thinking": "hmm"})
        out = event_to_output(event, "sess-1")
        assert out.type == "thinking"
        assert out.thinking == "hmm"
        assert out.status == "success"

    def test_tool_use_event(self):
        event = AgentEvent(
            type="tool_use",
            data={"tool_name": "bash", "tool_input": {"command": "ls"}},
        )
        out = event_to_output(event, None)
        assert out.type == "tool_use"
        assert out.tool_name == "bash"
        assert out.tool_input == {"command": "ls"}

    def test_tool_result_event(self):
        event = AgentEvent(
            type="tool_result",
            data={
                "tool_result_id": "tr-1",
                "tool_result_content": "ok",
                "tool_result_is_error": False,
            },
        )
        out = event_to_output(event, None)
        assert out.type == "tool_result"
        assert out.tool_result_id == "tr-1"
        assert out.tool_result_is_error is False

    def test_text_event(self):
        event = AgentEvent(type="text", data={"text": "hello"})
        out = event_to_output(event, None)
        assert out.type == "text"
        assert out.text == "hello"

    def test_system_event(self):
        event = AgentEvent(
            type="system",
            data={"system_subtype": "init", "system_data": {"session_id": "s1"}},
        )
        out = event_to_output(event, None)
        assert out.type == "system"
        assert out.system_subtype == "init"
        assert out.system_data == {"session_id": "s1"}

    def test_result_event_includes_session(self):
        event = AgentEvent(
            type="result",
            data={"result": "Final answer", "result_metadata": {"cost": 0.01}},
        )
        out = event_to_output(event, "sess-42")
        assert out.type == "result"
        assert out.status == "success"
        assert out.result == "Final answer"
        assert out.new_session_id == "sess-42"
        assert out.result_metadata == {"cost": 0.01}
        assert out.error is None

    def test_result_event_with_is_error(self):
        """SDK is_error=True should produce status='error' with error field set."""
        error_text = 'API Error: 429 {"error":{"type":"rate_limit_error"}}'
        event = AgentEvent(
            type="result",
            data={
                "result": error_text,
                "result_metadata": {"is_error": True, "num_turns": 0},
            },
        )
        out = event_to_output(event, "sess-99")
        assert out.status == "error"
        assert out.error == error_text
        assert out.result == error_text
        assert out.new_session_id == "sess-99"

    def test_unknown_event_type(self):
        event = AgentEvent(type="unknown_type", data={})
        out = event_to_output(event, None)
        assert out.type == "text"
        assert out.status == "success"


# ---------------------------------------------------------------------------
# should_close
# ---------------------------------------------------------------------------


class TestShouldClose:
    """Test _close sentinel detection."""

    def test_no_sentinel(self, tmp_path):
        with patch("agent_runner.ipc.IPC_INPUT_CLOSE_SENTINEL", tmp_path / "_close"):
            assert should_close() is False

    def test_sentinel_exists(self, tmp_path):
        sentinel = tmp_path / "_close"
        sentinel.touch()
        with patch("agent_runner.ipc.IPC_INPUT_CLOSE_SENTINEL", sentinel):
            assert should_close() is True
            # Sentinel should be cleaned up
            assert not sentinel.exists()


# ---------------------------------------------------------------------------
# drain_ipc_input
# ---------------------------------------------------------------------------


class TestDrainIpcInput:
    """Test IPC input message draining."""

    def test_empty_directory(self, tmp_path):
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []

    def test_single_message(self, tmp_path):
        msg_file = tmp_path / "001.json"
        msg_file.write_text(json.dumps({"type": "message", "text": "hello"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == ["hello"]
            assert not msg_file.exists()  # File should be consumed

    def test_multiple_messages_sorted(self, tmp_path):
        (tmp_path / "002.json").write_text(json.dumps({"type": "message", "text": "second"}))
        (tmp_path / "001.json").write_text(json.dumps({"type": "message", "text": "first"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == ["first", "second"]

    def test_skips_non_message_types(self, tmp_path):
        (tmp_path / "001.json").write_text(json.dumps({"type": "other", "text": "ignored"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []

    def test_skips_messages_without_text(self, tmp_path):
        (tmp_path / "001.json").write_text(json.dumps({"type": "message"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []

    def test_handles_malformed_json(self, tmp_path):
        (tmp_path / "001.json").write_text("not json")
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == []
            assert not (tmp_path / "001.json").exists()  # Bad file cleaned up

    def test_ignores_non_json_files(self, tmp_path):
        (tmp_path / "readme.txt").write_text("not a message")
        (tmp_path / "001.json").write_text(json.dumps({"type": "message", "text": "hi"}))
        with patch("agent_runner.ipc.IPC_INPUT_DIR", tmp_path):
            result = drain_ipc_input()
            assert result == ["hi"]
            assert (tmp_path / "readme.txt").exists()  # Non-JSON untouched


# ---------------------------------------------------------------------------
# build_core_config
# ---------------------------------------------------------------------------


class TestBuildCoreConfig:
    """Test AgentCoreConfig construction from ContainerInput."""

    @staticmethod
    def _make_input(**overrides) -> ContainerInput:
        data = {
            "messages": [],
            "group_folder": "test-group",
            "chat_jid": "123@g.us",
            "is_admin": True,
            **overrides,
        }
        return ContainerInput.from_dict(data)

    def test_admin_group_cwd(self):
        ci = self._make_input(is_admin=True)
        config = build_core_config(ci)
        assert config.cwd == "/workspace/project"

    def test_non_admin_with_repo_access_cwd(self):
        ci = self._make_input(is_admin=False, repo_access="owner/pynchy")
        config = build_core_config(ci)
        assert config.cwd == "/workspace/project"

    def test_non_admin_without_repo_access_cwd(self):
        ci = self._make_input(is_admin=False)
        config = build_core_config(ci)
        assert config.cwd == "/workspace/group"

    def test_mcp_servers_include_pynchy(self):
        ci = self._make_input()
        config = build_core_config(ci)
        assert "pynchy" in config.mcp_servers
        assert config.mcp_servers["pynchy"]["command"] == "python"

    def test_mcp_env_includes_chat_jid(self):
        ci = self._make_input(chat_jid="456@g.us")
        config = build_core_config(ci)
        env = config.mcp_servers["pynchy"]["env"]
        assert env["PYNCHY_CHAT_JID"] == "456@g.us"

    def test_mcp_env_is_admin_flag(self):
        ci = self._make_input(is_admin=True)
        config = build_core_config(ci)
        assert config.mcp_servers["pynchy"]["env"]["PYNCHY_IS_ADMIN"] == "1"

        ci = self._make_input(is_admin=False)
        config = build_core_config(ci)
        assert config.mcp_servers["pynchy"]["env"]["PYNCHY_IS_ADMIN"] == "0"

    def test_mcp_env_scheduled_task_flag(self):
        ci = self._make_input(is_scheduled_task=True)
        config = build_core_config(ci)
        assert config.mcp_servers["pynchy"]["env"]["PYNCHY_IS_SCHEDULED_TASK"] == "1"

    def test_system_notices_not_in_system_prompt(self):
        """System notices must NOT go in system_prompt_append — that would
        invalidate the KV cache on every session resume. They're prepended
        to the user prompt in main() instead."""
        ci = self._make_input(
            is_admin=False,
            system_notices=["Warning: repo dirty"],
        )
        config = build_core_config(ci)
        assert config.system_prompt_append is None

    def test_session_id_passed_through(self):
        ci = self._make_input(session_id="sess-xyz")
        config = build_core_config(ci)
        assert config.session_id == "sess-xyz"

    def test_extra_config_passed_through(self):
        ci = self._make_input(agent_core_config={"model": "opus"})
        config = build_core_config(ci)
        assert config.extra == {"model": "opus"}
