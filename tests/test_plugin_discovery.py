"""Tests for plugin discovery system with pluggy."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pluggy
import pytest

from pynchy.plugin import get_plugin_manager


class TestPluginManager:
    """Tests for plugin manager initialization and discovery."""

    def test_plugin_manager_initialization(self):
        """Plugin manager initializes successfully."""
        pm = get_plugin_manager()
        assert pm is not None
        assert pm.project_name == "pynchy"

    def test_built_in_claude_plugin_registered(self):
        """Built-in Claude plugin is registered automatically."""
        pm = get_plugin_manager()

        cores = pm.hook.pynchy_agent_core_info()
        assert len(cores) >= 1

        claude_core = next((c for c in cores if c["name"] == "claude"), None)
        assert claude_core is not None
        assert claude_core["module"] == "agent_runner.cores.claude"
        assert claude_core["class_name"] == "ClaudeAgentCore"

    def test_plugin_manager_has_hookspecs(self):
        """Plugin manager has all expected hook specifications."""
        pm = get_plugin_manager()

        # Verify all hooks are available
        assert hasattr(pm.hook, "pynchy_agent_core_info")
        assert hasattr(pm.hook, "pynchy_mcp_server_spec")
        assert hasattr(pm.hook, "pynchy_skill_paths")
        assert hasattr(pm.hook, "pynchy_create_channel")

    def test_multiple_plugin_manager_calls(self):
        """Multiple calls to get_plugin_manager work correctly."""
        pm1 = get_plugin_manager()
        pm2 = get_plugin_manager()

        # Each call creates a new manager instance
        assert pm1 is not pm2

        # But both have the same plugins
        cores1 = pm1.hook.pynchy_agent_core_info()
        cores2 = pm2.hook.pynchy_agent_core_info()
        assert len(cores1) == len(cores2)


class TestCustomPluginRegistration:
    """Tests for registering custom plugins."""

    def test_register_agent_core_plugin(self):
        """Register a custom agent core plugin."""
        hookimpl = pluggy.HookimplMarker("pynchy")

        class CustomCorePlugin:
            @hookimpl
            def pynchy_agent_core_info(self):
                return {
                    "name": "custom",
                    "module": "custom.core",
                    "class_name": "CustomCore",
                    "packages": [],
                    "host_source_path": None,
                }

        pm = get_plugin_manager()
        pm.register(CustomCorePlugin(), name="custom-plugin")

        cores = pm.hook.pynchy_agent_core_info()
        custom_core = next((c for c in cores if c["name"] == "custom"), None)

        assert custom_core is not None
        assert custom_core["module"] == "custom.core"

    def test_register_mcp_plugin(self):
        """Register a custom MCP server plugin."""
        hookimpl = pluggy.HookimplMarker("pynchy")

        class CustomMcpPlugin:
            @hookimpl
            def pynchy_mcp_server_spec(self):
                return {
                    "name": "custom-mcp",
                    "command": "python",
                    "args": ["-m", "custom.mcp"],
                    "env": {},
                    "host_source": None,
                }

        pm = get_plugin_manager()
        pm.register(CustomMcpPlugin(), name="custom-mcp-plugin")

        specs = pm.hook.pynchy_mcp_server_spec()
        custom_spec = next((s for s in specs if s["name"] == "custom-mcp"), None)

        assert custom_spec is not None
        assert custom_spec["command"] == "python"

    def test_register_skill_plugin(self):
        """Register a custom skill plugin."""
        hookimpl = pluggy.HookimplMarker("pynchy")

        class CustomSkillPlugin:
            @hookimpl
            def pynchy_skill_paths(self):
                return ["/path/to/skills"]

        pm = get_plugin_manager()
        pm.register(CustomSkillPlugin(), name="custom-skill-plugin")

        skill_path_lists = pm.hook.pynchy_skill_paths()
        # Result is list of lists
        assert len(skill_path_lists) >= 1

        # Find our plugin's paths
        custom_paths = next((paths for paths in skill_path_lists if "/path/to/skills" in paths), None)
        assert custom_paths is not None

    def test_register_multi_category_plugin(self):
        """Single plugin can implement multiple hooks."""
        hookimpl = pluggy.HookimplMarker("pynchy")

        class MultiPlugin:
            @hookimpl
            def pynchy_agent_core_info(self):
                return {
                    "name": "multi",
                    "module": "multi.core",
                    "class_name": "MultiCore",
                    "packages": [],
                    "host_source_path": None,
                }

            @hookimpl
            def pynchy_skill_paths(self):
                return ["/multi/skills"]

        pm = get_plugin_manager()
        pm.register(MultiPlugin(), name="multi-plugin")

        # Plugin appears in both hooks
        cores = pm.hook.pynchy_agent_core_info()
        multi_core = next((c for c in cores if c["name"] == "multi"), None)
        assert multi_core is not None

        skill_path_lists = pm.hook.pynchy_skill_paths()
        multi_paths = next((paths for paths in skill_path_lists if "/multi/skills" in paths), None)
        assert multi_paths is not None


class TestHookCalling:
    """Tests for hook calling strategies."""

    def test_agent_core_hook_returns_all_results(self):
        """Agent core hook returns results from all plugins."""
        hookimpl = pluggy.HookimplMarker("pynchy")

        class Plugin1:
            @hookimpl
            def pynchy_agent_core_info(self):
                return {"name": "core1", "module": "m1", "class_name": "C1", "packages": [], "host_source_path": None}

        class Plugin2:
            @hookimpl
            def pynchy_agent_core_info(self):
                return {"name": "core2", "module": "m2", "class_name": "C2", "packages": [], "host_source_path": None}

        pm = get_plugin_manager()
        pm.register(Plugin1(), name="plugin1")
        pm.register(Plugin2(), name="plugin2")

        cores = pm.hook.pynchy_agent_core_info()

        # Results from all plugins (including built-in Claude)
        assert len(cores) >= 3
        names = [c["name"] for c in cores]
        assert "core1" in names
        assert "core2" in names
        assert "claude" in names

    def test_empty_hook_returns_empty_list(self):
        """Hook with no implementations returns empty list."""
        pm = get_plugin_manager()

        # MCP and skill hooks have no built-in implementations
        mcp_specs = pm.hook.pynchy_mcp_server_spec()
        skill_paths = pm.hook.pynchy_skill_paths()

        assert isinstance(mcp_specs, list)
        assert isinstance(skill_paths, list)
        # They should be empty if no plugins provide them
        assert len(mcp_specs) == 0
        assert len(skill_paths) == 0

    def test_plugin_blocking(self):
        """Plugins can be blocked from calling."""
        hookimpl = pluggy.HookimplMarker("pynchy")

        class BlockablePlugin:
            @hookimpl
            def pynchy_agent_core_info(self):
                return {"name": "blockable", "module": "m", "class_name": "C", "packages": [], "host_source_path": None}

        pm = get_plugin_manager()
        pm.register(BlockablePlugin(), name="blockable-plugin")

        # Before blocking
        cores_before = pm.hook.pynchy_agent_core_info()
        names_before = [c["name"] for c in cores_before]
        assert "blockable" in names_before

        # Block the plugin
        pm.set_blocked("blockable-plugin")

        # After blocking
        cores_after = pm.hook.pynchy_agent_core_info()
        names_after = [c["name"] for c in cores_after]
        assert "blockable" not in names_after


class TestPluginErrors:
    """Tests for plugin error handling."""

    def test_plugin_with_invalid_hook_signature(self):
        """Plugin with wrong hook signature raises error on registration."""
        hookimpl = pluggy.HookimplMarker("pynchy")

        class BadPlugin:
            @hookimpl
            def pynchy_agent_core_info(self, invalid_param):
                # This signature doesn't match the hookspec
                return {"name": "bad"}

        pm = get_plugin_manager()

        # Pluggy catches signature mismatches
        with pytest.raises(Exception):
            pm.register(BadPlugin(), name="bad-plugin")
            # Trigger validation by calling the hook
            pm.hook.pynchy_agent_core_info()

    def test_hook_with_no_plugins_still_callable(self):
        """Hooks with no implementations are still callable."""
        pm = get_plugin_manager()

        # These hooks have no built-in implementations
        # but should still be callable without errors
        mcp_specs = pm.hook.pynchy_mcp_server_spec()
        skill_paths = pm.hook.pynchy_skill_paths()
        channel = pm.hook.pynchy_create_channel(context=None)

        assert mcp_specs == []
        assert skill_paths == []
        assert channel is None  # firstresult returns None if no results
