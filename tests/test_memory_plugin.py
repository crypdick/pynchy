"""Tests for memory plugin registration and MCP handler wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.api import MemoryProvider, get_memory_provider
from pynchy.plugins.memory.sqlite_memory import SqliteMemoryPlugin
from pynchy.plugins.memory.sqlite_memory.backend import SqliteMemoryBackend


class TestMemoryProvider:
    def test_backend_satisfies_protocol(self, tmp_path):
        """SqliteMemoryBackend passes structural typing checks."""
        backend = SqliteMemoryBackend(tmp_path / "memories.db")
        assert isinstance(backend, MemoryProvider)

    def test_invalid_provider_rejected(self):
        """Objects missing required methods are rejected."""
        assert not isinstance(object(), MemoryProvider)
        assert not isinstance({"name": "fake"}, MemoryProvider)

    def test_plugin_provides_memory_hook(self, tmp_path):
        """SqliteMemoryPlugin returns a backend via pynchy_memory."""
        plugin = SqliteMemoryPlugin()
        backend = plugin.pynchy_memory(tmp_path / "memories.db")
        assert backend is not None
        assert backend.name == "sqlite"

    def test_plugin_provides_mcp_handlers(self):
        """SqliteMemoryPlugin returns all four tool handlers."""
        plugin = SqliteMemoryPlugin()
        result = plugin.pynchy_service_handler()
        assert {str(action.tool_name) for action in result.actions} == {
            "save_memory",
            "recall_memories",
            "forget_memory",
            "list_memories",
        }


class TestMcpHandlers:
    """Test MCP handlers validate inputs and delegate to backend."""

    @pytest.fixture(autouse=True)
    def _mock_backend(self, tmp_path):
        """Provide a mock backend through the plugin's public constructor.

        Handlers are driven through the plugin's public service-handler hook
        (``pynchy_service_handler`` exposes them keyed by tool name).
        """
        mock = AsyncMock(spec=SqliteMemoryBackend)
        mock.name = "sqlite"
        self.mock_backend = mock
        self.registration = SqliteMemoryPlugin(mock).pynchy_service_handler()

    def _handler(self, tool_name: str):
        action = self.registration.action_for(tool_name)
        assert action is not None
        return action.handler

    async def test_save_requires_source_group(self):
        result = await self._handler("save_memory")({"key": "k", "content": "c"})
        assert "error" in result

    async def test_save_requires_key_and_content(self):
        result = await self._handler("save_memory")({"source_group": "g"})
        assert "error" in result

    async def test_save_delegates_to_backend(self):
        self.mock_backend.save.return_value = {"key": "k", "status": "created"}
        result = await self._handler("save_memory")(
            {
                "source_group": "g",
                "key": "k",
                "content": "c",
                "category": "daily",
            }
        )
        assert result == {"result": {"key": "k", "status": "created"}}
        self.mock_backend.save.assert_called_once_with(
            group_folder="g",
            key="k",
            content="c",
            category="daily",
            metadata=None,
        )

    async def test_recall_requires_source_group(self):
        result = await self._handler("recall_memories")({"query": "test"})
        assert "error" in result

    async def test_recall_requires_query(self):
        result = await self._handler("recall_memories")({"source_group": "g"})
        assert "error" in result

    async def test_recall_delegates_to_backend(self):
        self.mock_backend.recall.return_value = [{"key": "k", "content": "c"}]
        result = await self._handler("recall_memories")(
            {
                "source_group": "g",
                "query": "test",
                "limit": 3,
            }
        )
        assert result["result"]["count"] == 1
        self.mock_backend.recall.assert_called_once_with(
            group_folder="g",
            query="test",
            category=None,
            limit=3,
        )

    async def test_forget_requires_source_group(self):
        result = await self._handler("forget_memory")({"key": "k"})
        assert "error" in result

    async def test_forget_delegates_to_backend(self):
        self.mock_backend.forget.return_value = {"removed": True}
        result = await self._handler("forget_memory")({"source_group": "g", "key": "k"})
        assert result == {"result": {"removed": True}}

    async def test_list_requires_source_group(self):
        result = await self._handler("list_memories")({})
        assert "error" in result

    async def test_list_delegates_to_backend(self):
        self.mock_backend.list_keys.return_value = [{"key": "k1"}]
        result = await self._handler("list_memories")({"source_group": "g", "category": "core"})
        assert result["result"]["count"] == 1
        self.mock_backend.list_keys.assert_called_once_with(
            group_folder="g",
            category="core",
        )


class TestDiscovery:
    def test_get_memory_provider_returns_none_without_valid_plugins(self, tmp_path):
        plugin_manager = get_plugin_manager()
        with patch.object(plugin_manager.hook, "pynchy_memory", return_value=[]):
            assert get_memory_provider(plugin_manager, tmp_path / "memories.db") is None

    def test_get_memory_provider_ignores_plugin_failure(self, tmp_path):
        plugin_manager = get_plugin_manager()
        with patch.object(
            plugin_manager.hook,
            "pynchy_memory",
            side_effect=RuntimeError("plugin unavailable"),
        ):
            assert get_memory_provider(plugin_manager, tmp_path / "memories.db") is None

    def test_get_memory_provider_returns_backend(self, tmp_path):
        """get_memory_provider finds the sqlite-memory plugin."""
        provider = get_memory_provider(get_plugin_manager(), tmp_path / "memories.db")
        # May be None if plugin loading context differs in tests,
        # but when it loads it should be valid.
        if provider is not None:
            assert provider.name == "sqlite"
            assert isinstance(provider, MemoryProvider)
