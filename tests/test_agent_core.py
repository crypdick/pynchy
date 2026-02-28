"""Tests for agent core protocol, registry, and events."""

import pytest

# Import from container module (will be available in test environment)
try:
    import sys
    from pathlib import Path

    # Add container agent_runner to path for testing
    container_path = Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"
    if container_path.exists():
        sys.path.insert(0, str(container_path))

    from agent_runner.core import AgentCore, AgentCoreConfig, AgentEvent
    from agent_runner.hooks import AGNOSTIC_TO_CLAUDE, CLAUDE_HOOK_MAP, HookEvent
    from agent_runner.registry import create_agent_core

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
            group_folder="admin-1",
            chat_jid="test@g.us",
            is_admin=True,
            is_scheduled_task=False,
            system_prompt_append="Test system prompt",
            mcp_servers={"pynchy": {"command": "python", "args": ["-m", "test"], "env": {}}},
            plugin_hooks=[],
            extra={"model": "claude-3-5-sonnet-20241022"},
        )

        assert config.cwd == "/workspace/project"
        assert config.session_id == "test-session-123"
        assert config.is_admin is True
        assert config.extra["model"] == "claude-3-5-sonnet-20241022"

    def test_agent_core_config_defaults(self):
        """Test AgentCoreConfig with default values."""
        config = AgentCoreConfig(
            cwd="/workspace/group",
            session_id=None,
            group_folder="test-group",
            chat_jid="test@g.us",
            is_admin=False,
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
    """Test agent core direct import and instantiation."""

    def test_create_claude_core(self):
        """Test creating Claude core instance via direct import."""
        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id=None,
            group_folder="admin-1",
            chat_jid="test@g.us",
            is_admin=True,
            is_scheduled_task=False,
        )

        try:
            core = create_agent_core("agent_runner.cores.claude", "ClaudeAgentCore", config)
            assert isinstance(core, AgentCore)
            assert core.session_id is None  # Not started yet
        except ImportError:
            pytest.skip("Claude SDK not available")

    def test_create_unknown_module_raises_error(self):
        """Test that importing unknown module raises ImportError."""
        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id=None,
            group_folder="admin-1",
            chat_jid="test@g.us",
            is_admin=True,
            is_scheduled_task=False,
        )

        with pytest.raises(ImportError, match="Failed to import agent core module"):
            create_agent_core("nonexistent.module", "NonexistentCore", config)

    def test_create_unknown_class_raises_error(self):
        """Test that accessing unknown class raises AttributeError."""
        config = AgentCoreConfig(
            cwd="/workspace/project",
            session_id=None,
            group_folder="admin-1",
            chat_jid="test@g.us",
            is_admin=True,
            is_scheduled_task=False,
        )

        with pytest.raises(AttributeError, match="has no class"):
            create_agent_core("agent_runner.core", "NonexistentClass", config)


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
        """Test ContainerInput has agent_core_module and agent_core_config fields."""
        from pynchy.types import ContainerInput

        input_data = ContainerInput(
            messages=[],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=True,
            agent_core_module="pynchy_core_openai.core",
            agent_core_class="OpenAIAgentCore",
            agent_core_config={"model": "gpt-4"},
        )

        assert input_data.agent_core_module == "pynchy_core_openai.core"
        assert input_data.agent_core_class == "OpenAIAgentCore"
        assert input_data.agent_core_config == {"model": "gpt-4"}

    def test_container_input_agent_core_defaults(self):
        """Test ContainerInput agent_core_module defaults to Claude."""
        from pynchy.types import ContainerInput

        input_data = ContainerInput(
            messages=[],
            group_folder="test",
            chat_jid="test@g.us",
            is_admin=True,
        )

        assert input_data.agent_core_module == "agent_runner.cores.claude"
        assert input_data.agent_core_class == "ClaudeAgentCore"
        assert input_data.agent_core_config is None


class TestAgentCorePlugin:
    """Test agent core plugin infrastructure with pluggy."""

    def test_plugin_manager_initialization(self):
        """Test plugin manager initializes with built-in Claude plugin."""
        from pynchy.plugins import get_plugin_manager

        pm = get_plugin_manager()
        assert pm is not None

        # Test that hook is callable
        cores = pm.hook.pynchy_agent_core_info()
        assert len(cores) >= 2

        # Verify Claude plugin is registered
        claude_core = next((c for c in cores if c["name"] == "claude"), None)
        assert claude_core is not None
        assert claude_core["module"] == "agent_runner.cores.claude"
        assert claude_core["class_name"] == "ClaudeAgentCore"
        assert claude_core["packages"] == []
        assert claude_core["host_source_path"] is None

    def test_custom_plugin_registration(self):
        """Test registering a custom agent core plugin."""
        import pluggy

        from pynchy.plugins import get_plugin_manager

        hookimpl = pluggy.HookimplMarker("pynchy")

        class TestCorePlugin:
            @hookimpl
            def pynchy_agent_core_info(self):
                return {
                    "name": "test-core",
                    "module": "test_module.core",
                    "class_name": "TestAgentCore",
                    "packages": [],
                    "host_source_path": None,
                }

        pm = get_plugin_manager()
        pm.register(TestCorePlugin(), name="test-plugin")

        cores = pm.hook.pynchy_agent_core_info()
        test_core = next((c for c in cores if c["name"] == "test-core"), None)

        assert test_core is not None
        assert test_core["module"] == "test_module.core"
        assert test_core["class_name"] == "TestAgentCore"

    def test_plugin_with_packages(self):
        """Test plugin returning container packages."""
        import pluggy

        from pynchy.plugins import get_plugin_manager

        hookimpl = pluggy.HookimplMarker("pynchy")

        class OpenAICorePlugin:
            @hookimpl
            def pynchy_agent_core_info(self):
                return {
                    "name": "openai",
                    "module": "pynchy_core_openai.core",
                    "class_name": "OpenAIAgentCore",
                    "packages": ["openai>=1.0.0"],
                    "host_source_path": None,
                }

        pm = get_plugin_manager()
        pm.register(OpenAICorePlugin(), name="openai-plugin")

        cores = pm.hook.pynchy_agent_core_info()
        openai_core = next((c for c in cores if c["name"] == "openai"), None)

        assert openai_core is not None
        assert openai_core["packages"] == ["openai>=1.0.0"]
