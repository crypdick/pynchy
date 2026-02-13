"""Tests for plugin discovery system."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from pynchy.plugin import PluginBase, PluginRegistry, discover_plugins


class MockPlugin(PluginBase):
    """Mock plugin for testing."""

    def __init__(self, name: str = "test", categories: list[str] | None = None):
        self.name = name
        self.version = "0.1.0"
        self.categories = categories if categories is not None else ["mcp"]
        self.description = "Test plugin"


class TestPluginBase:
    """Tests for PluginBase validation."""

    def test_validate_succeeds_with_valid_plugin(self):
        """Valid plugin passes validation."""
        plugin = MockPlugin(name="test", categories=["mcp"])
        plugin.validate()  # Should not raise

    def test_validate_fails_without_name(self):
        """Plugin without name fails validation."""
        plugin = MockPlugin(name="", categories=["mcp"])
        with pytest.raises(ValueError, match="Plugin must have a name"):
            plugin.validate()

    def test_validate_fails_without_categories(self):
        """Plugin without categories fails validation."""
        plugin = MockPlugin(name="test", categories=[])
        with pytest.raises(ValueError, match="Plugin must declare at least one category"):
            plugin.validate()

    def test_validate_fails_with_invalid_category(self):
        """Plugin with invalid category fails validation."""
        plugin = MockPlugin(name="test", categories=["invalid"])
        with pytest.raises(ValueError, match="Invalid category 'invalid'"):
            plugin.validate()

    def test_validate_allows_valid_categories(self):
        """All valid categories are accepted."""
        valid_categories = ["runtime", "channel", "mcp", "skill", "hook"]
        for category in valid_categories:
            plugin = MockPlugin(name="test", categories=[category])
            plugin.validate()  # Should not raise

    def test_validate_allows_multiple_categories(self):
        """Plugin can have multiple categories."""
        plugin = MockPlugin(name="test", categories=["channel", "mcp"])
        plugin.validate()  # Should not raise


class TestPluginRegistry:
    """Tests for PluginRegistry."""

    def test_registry_starts_empty(self):
        """New registry has empty lists."""
        registry = PluginRegistry()
        assert registry.all_plugins == []
        assert registry.runtimes == []
        assert registry.channels == []
        assert registry.mcp_servers == []
        assert registry.skills == []
        assert registry.hooks == []

    def test_registry_can_hold_plugins(self):
        """Registry can store plugins in lists."""
        registry = PluginRegistry()
        plugin = MockPlugin(name="test", categories=["mcp"])

        registry.all_plugins.append(plugin)
        registry.mcp_servers.append(plugin)

        assert len(registry.all_plugins) == 1
        assert len(registry.mcp_servers) == 1
        assert registry.all_plugins[0] == plugin
        assert registry.mcp_servers[0] == plugin


class TestDiscoverPlugins:
    """Tests for discover_plugins function."""

    def test_discover_with_no_plugins(self):
        """Discovery with no plugins returns empty registry."""
        with patch("pynchy.plugin.entry_points", return_value=[]):
            registry = discover_plugins()

        assert len(registry.all_plugins) == 0
        assert len(registry.runtimes) == 0
        assert len(registry.channels) == 0
        assert len(registry.mcp_servers) == 0
        assert len(registry.skills) == 0
        assert len(registry.hooks) == 0

    def test_discover_single_plugin(self):
        """Discovery finds and registers a single plugin."""
        mock_ep = Mock()
        mock_ep.name = "test-plugin"
        mock_ep.load.return_value = lambda: MockPlugin(name="test", categories=["mcp"])

        with patch("pynchy.plugin.entry_points", return_value=[mock_ep]):
            registry = discover_plugins()

        assert len(registry.all_plugins) == 1
        assert len(registry.mcp_servers) == 1
        assert registry.all_plugins[0].name == "test"
        assert registry.mcp_servers[0].name == "test"

    def test_discover_multiple_plugins(self):
        """Discovery finds multiple plugins."""
        mock_ep1 = Mock()
        mock_ep1.name = "plugin1"
        mock_ep1.load.return_value = lambda: MockPlugin(name="plugin1", categories=["mcp"])

        mock_ep2 = Mock()
        mock_ep2.name = "plugin2"
        mock_ep2.load.return_value = lambda: MockPlugin(name="plugin2", categories=["channel"])

        with patch("pynchy.plugin.entry_points", return_value=[mock_ep1, mock_ep2]):
            registry = discover_plugins()

        assert len(registry.all_plugins) == 2
        assert len(registry.mcp_servers) == 1
        assert len(registry.channels) == 1
        assert registry.mcp_servers[0].name == "plugin1"
        assert registry.channels[0].name == "plugin2"

    def test_discover_composite_plugin(self):
        """Composite plugin appears in multiple category lists."""
        mock_ep = Mock()
        mock_ep.name = "composite"
        mock_ep.load.return_value = lambda: MockPlugin(
            name="composite", categories=["channel", "mcp"]
        )

        with patch("pynchy.plugin.entry_points", return_value=[mock_ep]):
            registry = discover_plugins()

        assert len(registry.all_plugins) == 1
        assert len(registry.channels) == 1
        assert len(registry.mcp_servers) == 1
        # Same plugin instance in both lists
        assert registry.channels[0] == registry.mcp_servers[0]

    def test_discover_all_category_types(self):
        """Plugin can be registered in all category types."""
        mock_ep = Mock()
        mock_ep.name = "all-categories"
        mock_ep.load.return_value = lambda: MockPlugin(
            name="all", categories=["runtime", "channel", "mcp", "skill", "hook"]
        )

        with patch("pynchy.plugin.entry_points", return_value=[mock_ep]):
            registry = discover_plugins()

        assert len(registry.all_plugins) == 1
        assert len(registry.runtimes) == 1
        assert len(registry.channels) == 1
        assert len(registry.mcp_servers) == 1
        assert len(registry.skills) == 1
        assert len(registry.hooks) == 1

    def test_discover_skips_broken_plugin(self):
        """Broken plugin is logged and skipped, doesn't crash."""
        mock_ep_good = Mock()
        mock_ep_good.name = "good"
        mock_ep_good.load.return_value = lambda: MockPlugin(name="good", categories=["mcp"])

        mock_ep_broken = Mock()
        mock_ep_broken.name = "broken"
        mock_ep_broken.load.side_effect = RuntimeError("Plugin load failed")

        with patch("pynchy.plugin.entry_points", return_value=[mock_ep_good, mock_ep_broken]):
            registry = discover_plugins()

        # Good plugin is registered, broken is skipped
        assert len(registry.all_plugins) == 1
        assert registry.all_plugins[0].name == "good"

    def test_discover_skips_invalid_plugin(self):
        """Plugin that fails validation is skipped."""
        mock_ep_good = Mock()
        mock_ep_good.name = "good"
        mock_ep_good.load.return_value = lambda: MockPlugin(name="good", categories=["mcp"])

        mock_ep_invalid = Mock()
        mock_ep_invalid.name = "invalid"
        # Plugin with no categories fails validation
        mock_ep_invalid.load.return_value = lambda: MockPlugin(name="invalid", categories=[])

        with patch("pynchy.plugin.entry_points", return_value=[mock_ep_good, mock_ep_invalid]):
            registry = discover_plugins()

        # Only good plugin is registered
        assert len(registry.all_plugins) == 1
        assert registry.all_plugins[0].name == "good"

    def test_discover_uses_correct_entry_point_group(self):
        """Discovery queries the correct entry point group."""
        with patch("pynchy.plugin.entry_points") as mock_eps:
            mock_eps.return_value = []
            discover_plugins()

        mock_eps.assert_called_once_with(group="pynchy.plugins")
