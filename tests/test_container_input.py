"""Tests for container-side ContainerInput parsing."""

import sys
from unittest.mock import MagicMock

# Mock claude_agent_sdk so we can import container code on the host
sys.modules.setdefault("claude_agent_sdk", MagicMock())

sys.path.insert(0, "container/agent_runner/src")
from agent_runner.main import ContainerInput  # noqa: E402


class TestContainerInput:
    def test_parses_plugin_mcp_servers(self):
        data = {
            "messages": [{"message_type": "user", "content": "hi"}],
            "group_folder": "test",
            "chat_jid": "j@g.us",
            "is_admin": False,
            "plugin_mcp_servers": {"weather": {"command": "python", "args": ["-m", "weather"]}},
        }
        ci = ContainerInput(data)
        assert ci.plugin_mcp_servers == data["plugin_mcp_servers"]

    def test_plugin_mcp_servers_defaults_to_none(self):
        data = {
            "messages": [{"message_type": "user", "content": "hi"}],
            "group_folder": "test",
            "chat_jid": "j@g.us",
            "is_admin": False,
        }
        ci = ContainerInput(data)
        assert ci.plugin_mcp_servers is None
