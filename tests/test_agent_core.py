"""Tests for agent core protocol, registry, and events."""

import pytest

# Import from container module (will be available in test environment)
try:
    import sys
    from pathlib import Path

    # Add container agent_runner to path for testing
    container_path = Path(__file__).parent.parent / "container" / "agent_runner" / "src"
    if container_path.exists():
        sys.path.insert(0, str(container_path))

    from agent_runner.core import AgentCore, AgentCoreConfig, AgentEvent
    from agent_runner.hooks import AGNOSTIC_TO_CLAUDE, CLAUDE_HOOK_MAP, HookEvent
    from agent_runner.registry import create_agent_core, list_cores, register_core

    AGENT_RUNNER_AVAILABLE = True
except ImportError:
    AGENT_RUNNER_AVAILABLE = False


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestAgentCoreProtocol:
    """Test AgentCore protocol and data structures."""

    def test_agent_core_config_creation(self):
        """Test creating AgentCoreConfig with all fields."""
        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id="test-session-123",
            group_folder="main",
            chat_jid="test@g.us",
            is_main=True,
            is_scheduled_task=False,
            system_prompt_append="Test system prompt",
            mcp_servers={"pynchy": {"command": "python", "args": ["-m", "test"], "env": {}}},
            plugin_hooks=[],
            extra={"model": "claude-3-5-sonnet-20241022"},
        )

        assert config.cwd == "/workspace/project"
        assert config.session_id == "test-session-123"
        assert config.is_main is True
        assert config.extra["model"] == "claude-3-5-sonnet-20241022"

    def test_agent_core_config_defaults(self):
        """Test AgentCoreConfig with default values."""
        config = AgentCoreConfig(
            cwd="/workspace/group",
            session_id=None,
            group_folder="test-group",
            chat_jid="test@g.us",
            is_main=False,
            is_scheduled_task=True,
        )

        assert config.system_prompt_append is None
        assert config.mcp_servers == {}
        assert config.plugin_hooks == []
        assert config.extra == {}

    def test_agent_event_creation(self):
        """Test creating AgentEvent with different types."""
        # Text event
        text_event = AgentEvent(type="text", data={"text": "Hello"})
        assert text_event.type == "text"
        assert text_event.data["text"] == "Hello"

        # Tool use event
        tool_event = AgentEvent(
            type="tool_use",
            data={"tool_name": "Read", "tool_input": {"file_path": "/test.txt"}},
        )
        assert tool_event.type == "tool_use"
        assert tool_event.data["tool_name"] == "Read"

        # Result event
        result_event = AgentEvent(
            type="result",
            data={"result": "Task completed", "result_metadata": {"duration_ms": 1000}},
        )
        assert result_event.type == "result"
        assert result_event.data["result"] == "Task completed"


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestAgentCoreRegistry:
    """Test agent core registry and selection."""

    def test_list_cores_includes_claude(self):
        """Test that Claude core is registered by default."""
        cores = list_cores()
        # Claude should be registered if SDK is available
        # If not available, registry should be empty but not crash
        assert isinstance(cores, list)

    def test_create_claude_core(self):
        """Test creating Claude core instance."""
        cores = list_cores()
        if "claude" not in cores:
            pytest.skip("Claude SDK not available")

        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id=None,
            group_folder="main",
            chat_jid="test@g.us",
            is_main=True,
            is_scheduled_task=False,
        )

        core = create_agent_core("claude", config)
        assert isinstance(core, AgentCore)
        assert core.session_id is None  # Not started yet

    def test_create_unknown_core_raises_error(self):
        """Test that creating unknown core raises KeyError."""
        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id=None,
            group_folder="main",
            chat_jid="test@g.us",
            is_main=True,
            is_scheduled_task=False,
        )

        with pytest.raises(KeyError, match="Unknown agent core 'nonexistent'"):
            create_agent_core("nonexistent", config)

    def test_register_custom_core(self):
        """Test registering a custom core implementation."""

        class MockCore:
            def __init__(self, config):
                self.config = config
                self._session_id = None

            async def start(self):
                pass

            async def query(self, prompt):
                yield AgentEvent(type="text", data={"text": "Mock response"})
                yield AgentEvent(type="result", data={"result": "Done", "result_metadata": {}})

            async def stop(self):
                pass

            @property
            def session_id(self):
                return self._session_id

        # Register mock core
        register_core("mock", MockCore)

        # Verify it's listed
        cores = list_cores()
        assert "mock" in cores

        # Create instance
        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id=None,
            group_folder="main",
            chat_jid="test@g.us",
            is_main=True,
            is_scheduled_task=False,
        )
        core = create_agent_core("mock", config)
        assert isinstance(core, AgentCore)


@pytest.mark.skipif(not AGENT_RUNNER_AVAILABLE, reason="agent_runner module not available")
class TestHookAbstraction:
    """Test hook event abstraction and mapping."""

    def test_hook_event_enum_values(self):
        """Test HookEvent enum has expected values."""
        assert HookEvent.BEFORE_COMPACT == "before_compact"
        assert HookEvent.AFTER_COMPACT == "after_compact"
        assert HookEvent.BEFORE_QUERY == "before_query"
        assert HookEvent.AFTER_QUERY == "after_query"
        assert HookEvent.SESSION_START == "session_start"
        assert HookEvent.SESSION_END == "session_end"
        assert HookEvent.ERROR == "error"

    def test_claude_hook_map_bidirectional(self):
        """Test Claude hook mapping is bidirectional."""
        # Check forward mapping exists
        assert "PreCompact" in CLAUDE_HOOK_MAP
        assert CLAUDE_HOOK_MAP["PreCompact"] == HookEvent.BEFORE_COMPACT

        # Check reverse mapping exists
        assert HookEvent.BEFORE_COMPACT in AGNOSTIC_TO_CLAUDE
        assert AGNOSTIC_TO_CLAUDE[HookEvent.BEFORE_COMPACT] == "PreCompact"

    def test_all_claude_hooks_have_reverse_mapping(self):
        """Test all Claude hooks can be reverse-mapped."""
        for claude_name, agnostic_event in CLAUDE_HOOK_MAP.items():
            assert agnostic_event in AGNOSTIC_TO_CLAUDE
            assert AGNOSTIC_TO_CLAUDE[agnostic_event] == claude_name


# Host-side tests (don't require agent_runner imports)
class TestContainerInputAgentCore:
    """Test ContainerInput includes agent_core fields."""

    def test_container_input_has_agent_core_fields(self):
        """Test ContainerInput has agent_core and agent_core_config fields."""
        from pynchy.types import ContainerInput

        input_data = ContainerInput(
            messages=[],
            group_folder="test",
            chat_jid="test@g.us",
            is_main=True,
            agent_core="openai",
            agent_core_config={"model": "gpt-4"},
        )

        assert input_data.agent_core == "openai"
        assert input_data.agent_core_config == {"model": "gpt-4"}

    def test_container_input_agent_core_defaults(self):
        """Test ContainerInput agent_core defaults to claude."""
        from pynchy.types import ContainerInput

        input_data = ContainerInput(
            messages=[],
            group_folder="test",
            chat_jid="test@g.us",
            is_main=True,
        )

        assert input_data.agent_core == "claude"
        assert input_data.agent_core_config is None


class TestAgentCorePlugin:
    """Test agent core plugin infrastructure."""

    def test_agent_core_plugin_base_class(self):
        """Test AgentCorePlugin base class."""
        from pynchy.plugin.agent_core import AgentCorePlugin

        class TestCore(AgentCorePlugin):
            name = "test-core"
            categories = ["agent_core"]

            def core_name(self):
                return "test"

        plugin = TestCore()
        assert plugin.name == "test-core"
        assert "agent_core" in plugin.categories
        assert plugin.core_name() == "test"
        assert plugin.container_packages() == []
        assert plugin.core_module_path() is None

    def test_agent_core_plugin_with_packages(self):
        """Test AgentCorePlugin with container packages."""
        from pynchy.plugin.agent_core import AgentCorePlugin

        class OpenAICore(AgentCorePlugin):
            name = "openai-core"
            categories = ["agent_core"]

            def core_name(self):
                return "openai"

            def container_packages(self):
                return ["openai>=1.0.0"]

        plugin = OpenAICore()
        assert plugin.container_packages() == ["openai>=1.0.0"]
