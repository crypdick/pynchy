"""Tests for src/pynchy/agent/agent_runner/src/agent_runner/main.py.

Tests core functions: build_sdk_messages, event_to_output, ContainerOutput,
ContainerInput, should_close, drain_ipc_input, build_core_config.
"""

from __future__ import annotations

# We need to adjust the import path since agent_runner lives in src/pynchy/agent/
import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner.core import AgentCoreConfig
from agent_runner.host_direct import build_host_core_config
from agent_runner.main import (
    apply_followup_metadata,
    build_core_config,
)
from agent_runner.models import ContainerInput

# ---------------------------------------------------------------------------
# ContainerOutput.to_dict
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

    def test_admin_without_repo_access_cwd(self):
        ci = self._make_input(is_admin=True)
        config = build_core_config(ci)
        assert config.cwd == "/home/agent/workspace"

    def test_non_admin_with_repo_access_cwd(self):
        ci = self._make_input(is_admin=False, repo_access="owner/pynchy")
        config = build_core_config(ci)
        assert config.cwd == "/home/agent/src/owner/pynchy"

    def test_non_admin_without_repo_access_cwd(self):
        ci = self._make_input(is_admin=False)
        config = build_core_config(ci)
        assert config.cwd == "/home/agent/workspace"

    def test_mcp_servers_include_pynchy(self):
        ci = self._make_input()
        config = build_core_config(ci)
        assert "pynchy" in config.mcp_servers
        assert config.mcp_servers["pynchy"]["command"] == "python"

    def test_codex_limits_pynchy_mcp_to_agent_tool_grants(self):
        ci = self._make_input(
            agent_core_module="agent_runner.cores.codex",
            agent_tool_grants=["computer_use"],
        )

        server = build_core_config(ci).mcp_servers["pynchy"]

        assert server["required"] is True
        assert "computer_use" in server["enabled_tools"]
        assert "search_skills" in server["enabled_tools"]
        assert "list_calendars" not in server["enabled_tools"]

    def test_other_cores_keep_full_pynchy_mcp_server(self):
        ci = self._make_input(
            agent_core_module="agent_runner.cores.claude",
            agent_tool_grants=["computer_use"],
        )

        server = build_core_config(ci).mcp_servers["pynchy"]

        assert "enabled_tools" not in server
        assert "required" not in server

    def test_mcp_env_includes_chat_jid(self):
        ci = self._make_input(chat_jid="456@g.us")
        config = build_core_config(ci)
        env = config.mcp_servers["pynchy"]["env"]
        assert env["PYNCHY_CHAT_JID"] == "456@g.us"

    def test_mcp_env_includes_global_learned_skill_root(self, monkeypatch):
        monkeypatch.setenv("PYNCHY_SKILLS_ROOT", "/home/agent/skills")
        config = build_core_config(self._make_input())
        env = config.mcp_servers["pynchy"]["env"]

        assert env["PYNCHY_SKILLS_ROOT"] == "/home/agent/skills"
        assert "PYNCHY_PROFILE_SKILLS_ROOT" not in env

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

    def test_direct_sse_mcp_server_uses_sse_endpoint(self):
        ci = self._make_input(
            mcp_direct_servers=[{"name": "docs", "url": "http://mcp-docs:8000", "transport": "sse"}]
        )
        config = build_core_config(ci)
        assert config.mcp_servers["docs"] == {
            "type": "sse",
            "url": "http://mcp-docs:8000/sse",
        }

    def test_streamable_http_mcp_server_normalizes_to_http(self):
        ci = self._make_input(
            mcp_direct_servers=[
                {
                    "name": "search",
                    "url": "http://mcp-search:8000",
                    "transport": "streamable_http",
                }
            ]
        )
        config = build_core_config(ci)
        assert config.mcp_servers["search"] == {
            "type": "http",
            "url": "http://mcp-search:8000/mcp",
        }

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

    def test_plugin_hooks_passed_through(self, tmp_path):
        hook_path = tmp_path / "audit.py"
        ci = self._make_input(plugin_hooks=[{"name": "audit", "module_path": str(hook_path)}])

        config = build_core_config(ci)

        assert config.plugin_hooks == [{"name": "audit", "module_path": str(hook_path)}]

    def test_turn_metadata_added_to_extra_config(self):
        ci = self._make_input(turn_id="turn_1", agent_core_config={"model": "opus"})
        config = build_core_config(ci)
        assert config.extra["metadata"] == {
            "pynchy_turn_id": "turn_1",
            "pynchy_chat_jid": "123@g.us",
            "pynchy_group_folder": "test-group",
        }

    def test_followup_metadata_updates_warm_core_config(self):
        config = AgentCoreConfig(
            cwd="/home/agent/workspace",
            session_id="resp_1",
            group_folder="test-group",
            chat_jid="123@g.us",
            is_admin=True,
            is_scheduled_task=False,
            turn_id="turn_1",
            extra={"metadata": {"pynchy_turn_id": "turn_1", "stable": "yes"}},
        )

        apply_followup_metadata(
            config,
            turn_id="turn_2",
            metadata={"source": "warm"},
        )

        assert config.turn_id == "turn_2"
        assert config.extra["metadata"] == {
            "pynchy_turn_id": "turn_2",
            "pynchy_chat_jid": "123@g.us",
            "pynchy_group_folder": "test-group",
            "stable": "yes",
            "source": "warm",
        }


class TestBuildHostCoreConfig:
    """Test direct host AgentCoreConfig construction."""

    def test_host_core_uses_real_cwd_and_pynchy_mcp(self, monkeypatch, tmp_path):
        ipc_dir = tmp_path / "ipc"
        skills_root = tmp_path / "skills"
        hook_path = tmp_path / "audit.py"
        monkeypatch.setenv("PYNCHY_IPC_DIR", str(ipc_dir))
        monkeypatch.setenv("PYNCHY_SKILLS_ROOT", str(skills_root))
        ci = ContainerInput(
            messages=[],
            session_id="sess-1",
            turn_id="turn-host",
            query_id="query-host",
            group_folder="admin-host",
            chat_jid="slack:C123",
            is_admin=True,
            system_prompt_append="Prompt notes",
            is_scheduled_task=False,
            agent_core_module="agent_runner.cores.codex",
            agent_core_class="CodexCLIAgentCore",
            agent_core_config={"approval_policy": "never"},
            plugin_hooks=[{"name": "audit", "module_path": str(hook_path)}],
        )

        config = build_host_core_config(ci, cwd="/workspace/project")

        assert config.cwd == "/workspace/project"
        assert config.session_id == "sess-1"
        assert config.turn_id == "turn-host"
        assert config.extra["pynchy_query_id"] == "query-host"
        assert config.group_folder == "admin-host"
        assert config.chat_jid == "slack:C123"
        assert config.is_admin is True
        assert config.system_prompt_append == "Prompt notes"
        pynchy_mcp = config.mcp_servers["pynchy"]
        assert pynchy_mcp["args"] == ["-m", "agent_runner.agent_tools"]
        assert pynchy_mcp["env"] == {
            "PYNCHY_CHAT_JID": "slack:C123",
            "PYNCHY_GROUP_FOLDER": "admin-host",
            "PYNCHY_IS_ADMIN": "1",
            "PYNCHY_SESSION_ID": "sess-1",
            "PYNCHY_IS_SCHEDULED_TASK": "0",
            "PYNCHY_TURN_ID": "turn-host",
            "PYNCHY_IPC_DIR": str(ipc_dir),
            "PYNCHY_SKILLS_ROOT": str(skills_root),
        }
        assert config.plugin_hooks == [{"name": "audit", "module_path": str(hook_path)}]
        assert config.extra == {
            "approval_policy": "never",
            "pynchy_query_id": "query-host",
        }

    def test_host_core_routes_direct_mcp_servers_through_local_proxy(self):
        ci = ContainerInput(
            messages=[],
            group_folder="admin-host",
            chat_jid="slack:C123",
            is_admin=True,
            mcp_direct_servers=[
                {
                    "name": "linear",
                    "url": "http://192.168.64.1:9876/mcp/admin-host/0/linear",
                    "transport": "streamable_http",
                }
            ],
        )

        config = build_host_core_config(ci, cwd="/workspace/project")

        assert config.mcp_servers["linear"] == {
            "type": "http",
            "url": "http://localhost:9876/mcp/admin-host/0/linear/mcp",
        }
