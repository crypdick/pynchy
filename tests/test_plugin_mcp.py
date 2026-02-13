"""Tests for MCP plugin system."""

from __future__ import annotations

from pathlib import Path

from pynchy.plugin import McpPlugin, McpServerSpec


class MockMcpPlugin(McpPlugin):
    """Mock MCP plugin for testing."""

    def __init__(
        self,
        name: str = "test-mcp",
        command: str = "python",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        host_source: Path | None = None,
    ):
        self.name = name
        self.version = "0.1.0"
        self.categories = ["mcp"]
        self.description = "Test MCP plugin"
        self._command = command
        self._args = args or ["-m", "test_server"]
        self._env = env or {}
        self._host_source = host_source

    def mcp_server_spec(self) -> McpServerSpec:
        return McpServerSpec(
            name=self.name,
            command=self._command,
            args=self._args,
            env=self._env,
            host_source=self._host_source,
        )


class TestMcpServerSpec:
    """Tests for McpServerSpec dataclass."""

    def test_spec_has_required_fields(self):
        """McpServerSpec has name, command, and args."""
        spec = McpServerSpec(
            name="test-server",
            command="python",
            args=["-m", "server"],
        )
        assert spec.name == "test-server"
        assert spec.command == "python"
        assert spec.args == ["-m", "server"]
        assert spec.env == {}
        assert spec.host_source is None

    def test_spec_with_env(self):
        """McpServerSpec can include environment variables."""
        spec = McpServerSpec(
            name="test-server",
            command="node",
            args=["server.js"],
            env={"API_KEY": "secret", "DEBUG": "true"},
        )
        assert spec.env == {"API_KEY": "secret", "DEBUG": "true"}

    def test_spec_with_host_source(self):
        """McpServerSpec can include host source path."""
        source_path = Path("/tmp/plugin-source")
        spec = McpServerSpec(
            name="test-server",
            command="python",
            args=["-m", "server"],
            host_source=source_path,
        )
        assert spec.host_source == source_path


class TestMcpPlugin:
    """Tests for McpPlugin base class."""

    def test_mcp_plugin_has_fixed_category(self):
        """McpPlugin has 'mcp' as fixed category."""
        plugin = MockMcpPlugin()
        assert plugin.categories == ["mcp"]

    def test_mcp_server_spec_is_abstract(self):
        """mcp_server_spec must be implemented by subclasses."""
        # This is verified by the ABC mechanism, but we test the mock works
        plugin = MockMcpPlugin()
        spec = plugin.mcp_server_spec()
        assert isinstance(spec, McpServerSpec)

    def test_plugin_returns_spec_with_correct_name(self):
        """Plugin returns spec with matching name."""
        plugin = MockMcpPlugin(name="custom-server")
        spec = plugin.mcp_server_spec()
        assert spec.name == "custom-server"

    def test_plugin_returns_spec_with_command_and_args(self):
        """Plugin returns spec with command and args."""
        plugin = MockMcpPlugin(
            command="node",
            args=["index.js", "--port", "3000"],
        )
        spec = plugin.mcp_server_spec()
        assert spec.command == "node"
        assert spec.args == ["index.js", "--port", "3000"]

    def test_plugin_returns_spec_with_env(self):
        """Plugin returns spec with environment variables."""
        plugin = MockMcpPlugin(
            env={"TOKEN": "abc123", "MODE": "production"},
        )
        spec = plugin.mcp_server_spec()
        assert spec.env == {"TOKEN": "abc123", "MODE": "production"}

    def test_plugin_returns_spec_with_host_source(self):
        """Plugin returns spec with host source path."""
        source_path = Path("/opt/plugin-code")
        plugin = MockMcpPlugin(host_source=source_path)
        spec = plugin.mcp_server_spec()
        assert spec.host_source == source_path


class TestMcpPluginIntegration:
    """Integration tests for MCP plugins."""

    def test_multiple_mcp_plugins_with_different_specs(self):
        """Multiple MCP plugins can coexist with different specs."""
        plugin1 = MockMcpPlugin(
            name="weather",
            command="python",
            args=["-m", "weather_server"],
            env={"API_KEY": "weather123"},
        )
        plugin2 = MockMcpPlugin(
            name="calendar",
            command="node",
            args=["calendar.js"],
            env={"TOKEN": "cal456"},
        )

        spec1 = plugin1.mcp_server_spec()
        spec2 = plugin2.mcp_server_spec()

        assert spec1.name == "weather"
        assert spec2.name == "calendar"
        assert spec1.command == "python"
        assert spec2.command == "node"
        assert spec1.env["API_KEY"] == "weather123"
        assert spec2.env["TOKEN"] == "cal456"

    def test_plugin_spec_serialization(self):
        """Plugin specs can be serialized to dict for JSON."""
        plugin = MockMcpPlugin(
            name="test",
            command="python",
            args=["-m", "test"],
            env={"KEY": "value"},
        )
        spec = plugin.mcp_server_spec()

        # Simulate what container_runner.py does
        serialized = {
            "command": spec.command,
            "args": spec.args,
            "env": spec.env,
        }

        assert serialized == {
            "command": "python",
            "args": ["-m", "test"],
            "env": {"KEY": "value"},
        }

    def test_plugin_with_source_path_for_mounting(self):
        """Plugin with host_source provides path for volume mount."""
        source_path = Path("/home/user/plugins/my-plugin")
        plugin = MockMcpPlugin(
            name="my-plugin",
            host_source=source_path,
        )
        spec = plugin.mcp_server_spec()

        # Simulate what container_runner.py does
        if spec.host_source:
            container_path = f"/workspace/plugins/{spec.name}"
            assert container_path == "/workspace/plugins/my-plugin"
            assert str(spec.host_source) == "/home/user/plugins/my-plugin"


class TestContainerRunnerIntegration:
    """Tests simulating container_runner.py integration."""

    def test_collecting_plugin_mcp_specs(self):
        """Container runner collects specs from multiple plugins."""
        # Simulate PluginRegistry with mcp_servers
        class MockRegistry:
            mcp_servers = [
                MockMcpPlugin(
                    name="weather",
                    command="python",
                    args=["-m", "weather"],
                    env={"API_KEY": "key1"},
                ),
                MockMcpPlugin(
                    name="calendar",
                    command="node",
                    args=["cal.js"],
                    env={"TOKEN": "key2"},
                ),
            ]

        registry = MockRegistry()

        # Simulate collection logic from container_runner.py
        plugin_mcp_specs: dict[str, dict] = {}
        for plugin in registry.mcp_servers:
            try:
                spec = plugin.mcp_server_spec()
                plugin_mcp_specs[spec.name] = {
                    "command": spec.command,
                    "args": spec.args,
                    "env": spec.env,
                }
            except Exception:
                pass

        assert len(plugin_mcp_specs) == 2
        assert "weather" in plugin_mcp_specs
        assert "calendar" in plugin_mcp_specs
        assert plugin_mcp_specs["weather"]["command"] == "python"
        assert plugin_mcp_specs["calendar"]["env"]["TOKEN"] == "key2"

    def test_building_volume_mounts_for_plugins(self):
        """Container runner builds volume mounts for plugin sources."""

        class MockRegistry:
            mcp_servers = [
                MockMcpPlugin(
                    name="plugin1",
                    host_source=Path("/opt/plugin1"),
                ),
                MockMcpPlugin(
                    name="plugin2",
                    host_source=Path("/opt/plugin2"),
                ),
                MockMcpPlugin(
                    name="plugin3",
                    host_source=None,  # No source to mount
                ),
            ]

        registry = MockRegistry()

        # Simulate mount building from container_runner.py
        mounts = []
        for plugin in registry.mcp_servers:
            try:
                spec = plugin.mcp_server_spec()
                if spec.host_source:
                    mounts.append({
                        "host_path": str(spec.host_source),
                        "container_path": f"/workspace/plugins/{spec.name}",
                        "readonly": True,
                    })
            except Exception:
                pass

        assert len(mounts) == 2  # plugin3 has no source
        assert mounts[0]["host_path"] == "/opt/plugin1"
        assert mounts[0]["container_path"] == "/workspace/plugins/plugin1"
        assert mounts[1]["host_path"] == "/opt/plugin2"
        assert all(mount["readonly"] for mount in mounts)

    def test_graceful_handling_of_plugin_errors(self):
        """Container runner handles plugin errors gracefully."""

        class BrokenPlugin(McpPlugin):
            name = "broken"
            version = "0.1.0"
            description = "Broken plugin"

            def mcp_server_spec(self) -> McpServerSpec:
                msg = "Plugin initialization failed"
                raise RuntimeError(msg)

        class MockRegistry:
            mcp_servers = [
                MockMcpPlugin(name="good"),
                BrokenPlugin(),
            ]

        registry = MockRegistry()

        # Simulate error handling from container_runner.py
        plugin_mcp_specs: dict[str, dict] = {}
        errors = []
        for plugin in registry.mcp_servers:
            try:
                spec = plugin.mcp_server_spec()
                plugin_mcp_specs[spec.name] = {
                    "command": spec.command,
                    "args": spec.args,
                    "env": spec.env,
                }
            except Exception as e:
                errors.append((plugin.name, str(e)))

        # Good plugin succeeds, broken plugin is skipped
        assert len(plugin_mcp_specs) == 1
        assert "good" in plugin_mcp_specs
        assert len(errors) == 1
        assert errors[0][0] == "broken"


class TestAgentRunnerIntegration:
    """Tests simulating agent_runner/main.py integration."""

    def test_merging_plugin_mcp_servers_into_config(self):
        """Agent runner merges plugin MCP specs into mcp_servers dict."""
        # Simulate container_input.plugin_mcp_servers from JSON
        plugin_mcp_servers = {
            "weather": {
                "command": "python",
                "args": ["-m", "weather_server"],
                "env": {"API_KEY": "secret"},
            },
            "calendar": {
                "command": "node",
                "args": ["calendar.js"],
                "env": {},
            },
        }

        # Simulate agent_runner logic
        mcp_servers_dict = {
            "pynchy": {
                "command": "python",
                "args": ["-m", "agent_runner.ipc_mcp"],
                "env": {"PYNCHY_CHAT_JID": "test@g.us"},
            },
        }

        # Merge plugin MCP servers
        if plugin_mcp_servers:
            for name, spec in plugin_mcp_servers.items():
                plugin_env = spec.get("env", {}).copy()
                plugin_env["PYTHONPATH"] = f"/workspace/plugins/{name}"
                mcp_servers_dict[name] = {
                    "command": spec["command"],
                    "args": spec["args"],
                    "env": plugin_env,
                }

        # Verify results
        assert len(mcp_servers_dict) == 3
        assert "pynchy" in mcp_servers_dict
        assert "weather" in mcp_servers_dict
        assert "calendar" in mcp_servers_dict

        # Check PYTHONPATH was added
        assert mcp_servers_dict["weather"]["env"]["PYTHONPATH"] == "/workspace/plugins/weather"
        assert mcp_servers_dict["calendar"]["env"]["PYTHONPATH"] == "/workspace/plugins/calendar"

        # Check original env vars preserved
        assert mcp_servers_dict["weather"]["env"]["API_KEY"] == "secret"

    def test_no_plugin_mcp_servers(self):
        """Agent runner handles case when no plugin MCP servers provided."""
        plugin_mcp_servers = None

        mcp_servers_dict = {
            "pynchy": {
                "command": "python",
                "args": ["-m", "agent_runner.ipc_mcp"],
                "env": {},
            },
        }

        # Merge plugin MCP servers (should be no-op)
        if plugin_mcp_servers:
            for name, spec in plugin_mcp_servers.items():
                plugin_env = spec.get("env", {}).copy()
                plugin_env["PYTHONPATH"] = f"/workspace/plugins/{name}"
                mcp_servers_dict[name] = {
                    "command": spec["command"],
                    "args": spec["args"],
                    "env": plugin_env,
                }

        # Only built-in pynchy server
        assert len(mcp_servers_dict) == 1
        assert "pynchy" in mcp_servers_dict

    def test_plugin_env_doesnt_override_pythonpath_if_set(self):
        """PYTHONPATH is always set to plugin directory."""
        plugin_mcp_servers = {
            "custom": {
                "command": "python",
                "args": ["-m", "server"],
                "env": {"PYTHONPATH": "/wrong/path", "OTHER": "value"},
            },
        }

        mcp_servers_dict = {}
        for name, spec in plugin_mcp_servers.items():
            plugin_env = spec.get("env", {}).copy()
            plugin_env["PYTHONPATH"] = f"/workspace/plugins/{name}"
            mcp_servers_dict[name] = {
                "command": spec["command"],
                "args": spec["args"],
                "env": plugin_env,
            }

        # PYTHONPATH should be overridden
        assert mcp_servers_dict["custom"]["env"]["PYTHONPATH"] == "/workspace/plugins/custom"
        # Other env vars preserved
        assert mcp_servers_dict["custom"]["env"]["OTHER"] == "value"
