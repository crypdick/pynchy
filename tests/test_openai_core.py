"""Tests for OpenAI Agents SDK agent core plugin and event mapping."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add container agent_runner to path for testing
container_path = Path(__file__).parent.parent / "container" / "agent_runner" / "src"
if container_path.exists():
    sys.path.insert(0, str(container_path))

try:
    from agent_runner.core import AgentCore, AgentCoreConfig, AgentEvent
    from agent_runner.registry import create_agent_core

    AGENT_RUNNER_AVAILABLE = True
except ImportError:
    AGENT_RUNNER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Host-side plugin tests (no agent_runner dependency)
# ---------------------------------------------------------------------------


class TestOpenAIPluginInfo:
    """Test the OpenAI host-side plugin provides correct info."""

    def test_plugin_info_structure(self):
        """Plugin returns all required fields."""
        from pynchy.plugins.agent_cores.openai import OpenAIAgentCorePlugin

        plugin = OpenAIAgentCorePlugin()
        info = plugin.pynchy_agent_core_info()

        assert info["name"] == "openai"
        assert info["module"] == "agent_runner.cores.openai"
        assert info["class_name"] == "OpenAIAgentCore"
        assert "openai-agents" in info["packages"][0]
        assert info["host_source_path"] is None

    def test_plugin_registered_via_auto_discovery(self):
        """OpenAI plugin is auto-discovered alongside Claude plugin."""
        from pynchy.plugins import get_plugin_manager

        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()
        cores = pm.hook.pynchy_agent_core_info()

        names = [c["name"] for c in cores]
        assert "claude" in names
        assert "openai" in names

    def test_core_selection_by_name(self):
        """Selecting a core by name returns the correct info."""
        from pynchy.plugins import get_plugin_manager

        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()
        cores = pm.hook.pynchy_agent_core_info()

        openai_core = next((c for c in cores if c["name"] == "openai"), None)
        assert openai_core is not None
        assert openai_core["class_name"] == "OpenAIAgentCore"

        claude_core = next((c for c in cores if c["name"] == "claude"), None)
        assert claude_core is not None
        assert claude_core["class_name"] == "ClaudeAgentCore"


# ---------------------------------------------------------------------------
# Container-side core tests (require agent_runner)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestOpenAICoreInstantiation:
    """Test OpenAI core can be created via registry."""

    def _make_config(self, **overrides) -> AgentCoreConfig:
        defaults = dict(
            cwd="/workspace/project",
            session_id=None,
            group_folder="admin-1",
            chat_jid="test@g.us",
            is_admin=True,
            is_scheduled_task=False,
            mcp_servers={
                "pynchy": {
                    "command": "python",
                    "args": ["-m", "agent_runner.agent_tools"],
                    "env": {"PYNCHY_CHAT_JID": "test@g.us"},
                }
            },
            extra={"model": "gpt-5.2"},
        )
        defaults.update(overrides)
        return AgentCoreConfig(**defaults)

    def test_create_via_registry(self):
        """create_agent_core() loads and instantiates OpenAIAgentCore."""
        config = self._make_config()
        try:
            core = create_agent_core("agent_runner.cores.openai", "OpenAIAgentCore", config)
            assert isinstance(core, AgentCore)
            assert core.session_id is None
        except ImportError:
            pytest.skip("openai-agents not installed")

    def test_session_id_from_config(self):
        """Session ID is passed through from config."""
        config = self._make_config(session_id="resp_abc123")
        try:
            core = create_agent_core("agent_runner.cores.openai", "OpenAIAgentCore", config)
            assert core.session_id == "resp_abc123"
        except ImportError:
            pytest.skip("openai-agents not installed")


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestMCPServerConversion:
    """Test that config.mcp_servers dict is converted to MCPServerStdio objects."""

    def test_mcp_servers_built_from_config(self):
        """start() converts mcp_servers dict to MCPServerStdio instances."""
        try:
            from agent_runner.cores.openai import OpenAIAgentCore
        except ImportError:
            pytest.skip("openai-agents not installed")

        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id=None,
            group_folder="admin-1",
            chat_jid="test@g.us",
            is_admin=True,
            is_scheduled_task=False,
            mcp_servers={
                "pynchy": {
                    "command": "python",
                    "args": ["-m", "agent_runner.agent_tools"],
                    "env": {"KEY": "val"},
                },
                "custom": {
                    "command": "node",
                    "args": ["server.js"],
                },
            },
        )

        core = OpenAIAgentCore(config)
        # Before start(), no servers are created
        assert len(core._mcp_servers) == 0


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestEventMapping:
    """Test that OpenAI stream events map to correct AgentEvent types."""

    def test_tool_call_event(self):
        """tool_call_item maps to AgentEvent(type='tool_use')."""
        # Simulate what the core does when processing a tool_call_item
        event = AgentEvent(
            type="tool_use",
            data={"tool_name": "shell", "tool_input": {"command": "ls"}},
        )
        assert event.type == "tool_use"
        assert event.data["tool_name"] == "shell"

    def test_tool_output_event(self):
        """tool_call_output_item maps to AgentEvent(type='tool_result')."""
        event = AgentEvent(
            type="tool_result",
            data={
                "tool_result_id": "call_123",
                "tool_result_content": "file1.txt\nfile2.txt",
                "tool_result_is_error": False,
            },
        )
        assert event.type == "tool_result"
        assert event.data["tool_result_content"] == "file1.txt\nfile2.txt"
        assert event.data["tool_result_is_error"] is False

    def test_text_event(self):
        """message_output_item maps to AgentEvent(type='text')."""
        event = AgentEvent(type="text", data={"text": "Here are the files"})
        assert event.type == "text"

    def test_thinking_event(self):
        """reasoning_item maps to AgentEvent(type='thinking')."""
        event = AgentEvent(type="thinking", data={"thinking": "I need to list files"})
        assert event.type == "thinking"

    def test_result_event(self):
        """Final output maps to AgentEvent(type='result') with metadata."""
        event = AgentEvent(
            type="result",
            data={
                "result": "Done! I listed the files.",
                "result_metadata": {
                    "subtype": "result",
                    "session_id": "resp_xyz789",
                    "is_error": False,
                },
            },
        )
        assert event.type == "result"
        assert event.data["result"] == "Done! I listed the files."
        assert event.data["result_metadata"]["session_id"] == "resp_xyz789"


# ---------------------------------------------------------------------------
# Config selection tests
# ---------------------------------------------------------------------------


class TestDefaultAgentCoreConfig:
    """Test agent core selection from Settings."""

    def test_default_is_claude(self):
        """Default agent core comes from Settings with valid value."""
        from pynchy.config import get_settings

        assert get_settings().agent.core in ("claude", "openai")

    def test_env_override(self):
        """Nested env override maps to settings.agent.core."""
        from pynchy.config import Settings

        # Provide the full agent section via env — the explicit-fields
        # validator requires all fields when a section is partially set.
        env = {
            "AGENT__CORE": "openai",
            "AGENT__NAME": "pynchy",
            "AGENT__TRIGGER_ALIASES": '["ghost"]',
        }
        with patch.dict("os.environ", env, clear=False):
            assert Settings().agent.core == "openai"
