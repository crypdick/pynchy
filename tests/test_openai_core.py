"""Tests for OpenAI Agents SDK agent core plugin and event mapping."""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.config.api import Settings, get_settings
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.agent_cores.openai import OpenAIAgentCorePlugin

# Add container agent_runner to path for testing
container_path = Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"
if container_path.exists():
    sys.path.insert(0, str(container_path))

try:
    from agent_runner.core import AgentCore, AgentCoreConfig
    from agent_runner.cores.openai import build_mcp_server, extract_tool_call
    from agent_runner.events import (
        ResultEvent,
        ResultMetadata,
        TextEvent,
        ThinkingEvent,
        ToolResultEvent,
        ToolUseEvent,
    )
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
        plugin = OpenAIAgentCorePlugin()
        info = plugin.pynchy_agent_core_info()

        assert info.name == "openai"
        assert info.module == "agent_runner.cores.openai"
        assert info.class_name == "OpenAIAgentCore"
        assert "openai-agents" in info.packages[0]
        assert info.host_source_path is None

    def test_plugin_registered_via_auto_discovery(self):
        """OpenAI plugin is auto-discovered alongside Claude plugin."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()
        cores = pm.hook.pynchy_agent_core_info()

        names = [core.name for core in cores]
        assert "claude" in names
        assert "openai" in names

    def test_core_selection_by_name(self):
        """Selecting a core by name returns the correct info."""
        with patch("pluggy.PluginManager.load_setuptools_entrypoints", return_value=0):
            pm = get_plugin_manager()
        cores = pm.hook.pynchy_agent_core_info()

        openai_core = next((core for core in cores if core.name == "openai"), None)
        assert openai_core is not None
        assert openai_core.class_name == "OpenAIAgentCore"

        claude_core = next((core for core in cores if core.name == "claude"), None)
        assert claude_core is not None
        assert claude_core.class_name == "ClaudeAgentCore"


# ---------------------------------------------------------------------------
# Container-side core tests (require agent_runner)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestOpenAICoreInstantiation:
    """Test OpenAI core can be created via registry."""

    def _make_config(self, **overrides) -> AgentCoreConfig:
        defaults = {
            "cwd": "/home/agent/src/owner/project",
            "session_id": None,
            "group_folder": "admin-1",
            "chat_jid": "test@g.us",
            "is_admin": True,
            "is_scheduled_task": False,
            "mcp_servers": {
                "pynchy": {
                    "command": "python",
                    "args": ["-m", "agent_runner.agent_tools"],
                    "env": {"PYNCHY_CHAT_JID": "test@g.us"},
                }
            },
            "extra": {"model": "gpt-5.5"},
        }
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

    def test_build_mcp_server_stdio(self):
        built = build_mcp_server(
            "pynchy",
            {
                "command": "python",
                "args": ["-m", "agent_runner.agent_tools"],
                "env": {"KEY": "val"},
            },
        )

        assert built is not None
        assert built.name == "pynchy"

    def test_build_mcp_server_sse(self):
        built = build_mcp_server(
            "browser",
            {"type": "sse", "url": "http://browser:3000/mcp", "headers": {"X-Test": "1"}},
        )

        assert built is not None
        assert built.name == "browser"

    def test_build_mcp_server_streamable_http(self):
        built = build_mcp_server(
            "remote",
            {"type": "http", "url": "https://example.test/mcp", "headers": {"Auth": "token"}},
        )

        assert built is not None
        assert built.name == "remote"

    def test_build_mcp_server_rejects_unknown_transport(self):
        built = build_mcp_server("mystery", {"type": "udp", "url": "udp://example.test"})

        assert built is None


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestEventMapping:
    """Test that OpenAI stream events map to correct AgentEvent types."""

    def test_tool_call_event(self):
        """tool_call_item maps to the typed tool-use event."""
        # Simulate what the core does when processing a tool_call_item
        event = ToolUseEvent(tool_name="shell", tool_input={"command": "ls"})
        assert event.type == "tool_use"
        assert event.tool_name == "shell"

    def test_tool_output_event(self):
        """tool_call_output_item maps to the typed tool-result event."""
        event = ToolResultEvent(
            tool_result_id="call_123",
            tool_result_content="file1.txt\nfile2.txt",
            tool_result_is_error=False,
        )
        assert event.type == "tool_result"
        assert event.tool_result_content == "file1.txt\nfile2.txt"
        assert event.tool_result_is_error is False

    def test_text_event(self):
        """message_output_item maps to the typed text event."""
        event = TextEvent(text="Here are the files")
        assert event.type == "text"

    def test_thinking_event(self):
        """reasoning_item maps to the typed thinking event."""
        event = ThinkingEvent(thinking="I need to list files")
        assert event.type == "thinking"

    def test_result_event(self):
        """Final output maps to the typed result event with metadata."""
        event = ResultEvent(
            result="Done! I listed the files.",
            result_metadata=ResultMetadata(
                subtype="result", session_id="resp_xyz789", is_error=False
            ),
        )
        assert event.type == "result"
        assert event.result == "Done! I listed the files."
        assert event.result_metadata.session_id == "resp_xyz789"


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestOpenAIToolParsing:
    """Focused coverage for the OpenAI tool-call parser."""

    def test_extract_tool_call_reads_nested_data_mapping(self):
        raw = {"data": {"name": "search_docs", "input": {"query": "hooks"}}}
        tool_name, tool_input = extract_tool_call(raw)
        assert tool_name == "search_docs"
        assert tool_input == {"query": "hooks"}

    def test_extract_tool_call_builds_shell_input_from_nested_action(self):
        raw = {"data": {"action": {"type": "shell_call", "command": "git status"}}}
        tool_name, tool_input = extract_tool_call(raw)
        assert tool_name == "shell"
        assert tool_input == {"command": "git status"}


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestOpenAIQueryModel:
    """OpenAI query behavior for the configured model."""

    @staticmethod
    def _config(*, metadata: dict[str, str] | None = None) -> AgentCoreConfig:
        extra: dict[str, object] = {"model": "primary-model"}
        if metadata is not None:
            extra["metadata"] = metadata
        return AgentCoreConfig(
            cwd="/home/agent/src/owner/project",
            session_id=None,
            group_folder="admin-1",
            chat_jid="test@g.us",
            is_admin=True,
            is_scheduled_task=False,
            mcp_servers={},
            extra=extra,
        )

    async def _start_core(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        metadata: dict[str, str] | None = None,
    ) -> object:
        try:
            from agent_runner.cores.openai import (  # noqa: PLC0415 - optional SDK import controls skip behavior.
                OpenAIAgentCore,
            )
        except ImportError:
            pytest.skip("openai-agents not installed")

        core = OpenAIAgentCore(self._config(metadata=metadata))
        monkeypatch.setattr(core, "_make_agent", lambda _model: object())
        await core.start()
        return core

    @staticmethod
    async def _collect_events(core, prompt: str):
        return [event async for event in core.query(prompt)]

    @pytest.mark.asyncio
    async def test_query_passes_metadata_via_run_config(self, monkeypatch):
        try:
            from agent_runner.cores import openai as openai_core  # noqa: PLC0415
        except ImportError:
            pytest.skip("openai-agents not installed")

        calls: list[dict[str, object]] = []

        class FakeResult:
            last_response_id = "resp_1"
            final_output = "ok"

            async def stream_events(self):
                for event in ():
                    yield event

        def fake_run_streamed(
            _agent: object,
            *,
            input: str,  # noqa: A002 - mirrors the pinned SDK keyword.
            previous_response_id: str | None = None,
            auto_previous_response_id: bool = False,
            run_config: object | None = None,
        ) -> FakeResult:
            calls.append(
                {
                    "input": input,
                    "previous_response_id": previous_response_id,
                    "auto_previous_response_id": auto_previous_response_id,
                    "run_config": run_config,
                }
            )
            return FakeResult()

        monkeypatch.setattr(openai_core.Runner, "run_streamed", fake_run_streamed)

        core = await self._start_core(monkeypatch, metadata={"pynchy_turn_id": "turn_1"})
        events = await self._collect_events(core, "hello")

        assert events[-1].type == "result"
        assert calls[0]["previous_response_id"] is None
        assert calls[0]["auto_previous_response_id"] is True
        run_config = calls[0]["run_config"]
        assert run_config is not None
        assert run_config.model_settings.metadata == {"pynchy_turn_id": "turn_1"}
        assert run_config.trace_metadata == {"pynchy_turn_id": "turn_1"}
        await core.stop()

    def test_pinned_runner_signature_uses_run_config_not_metadata(self):
        try:
            from agent_runner.cores import openai as openai_core  # noqa: PLC0415
        except ImportError:
            pytest.skip("openai-agents not installed")

        params = inspect.signature(openai_core.Runner.run_streamed).parameters
        assert "run_config" in params
        assert "metadata" not in params


# ---------------------------------------------------------------------------
# Config selection tests
# ---------------------------------------------------------------------------


class TestDefaultAgentCoreConfig:
    """Test agent core selection from Settings."""

    def test_default_is_openai(self):
        """Default agent core comes from Settings with valid value."""
        assert get_settings().agent.default_core == "openai"

    def test_env_override(self):
        """Nested env override maps to settings.agent.default_core."""
        env = {
            "AGENT__DEFAULT_CORE": "openai",
        }
        with patch.dict("os.environ", env, clear=False):
            assert Settings().agent.default_core == "openai"
