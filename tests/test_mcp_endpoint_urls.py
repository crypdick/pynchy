"""MCP endpoint URL contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from pynchy.host.container_manager.mcp.resolution import McpInstance
from pynchy.plugins.api import McpServerConfig


@pytest.mark.parametrize(
    ("config", "port", "expected"),
    [
        pytest.param(
            McpServerConfig(type="url", url="https://example.test/mcp"),
            None,
            "https://example.test/mcp",
            id="url",
        ),
        pytest.param(
            McpServerConfig(type="script", command="tool", port=9100, transport="sse"),
            9101,
            "http://localhost:9101",
            id="script-sse",
        ),
        pytest.param(
            McpServerConfig(
                type="stdio",
                command="tool",
                port=9100,
                transport="streamable_http",
            ),
            9101,
            "http://localhost:9101/mcp",
            id="stdio-streamable-http",
        ),
        pytest.param(
            McpServerConfig(
                type="docker",
                image="image",
                port=9100,
                transport="streamable_http",
            ),
            None,
            "http://mcp-server:9100/mcp",
            id="docker-streamable-http",
        ),
        pytest.param(
            McpServerConfig(type="docker", image="image", port=9100, transport="sse"),
            None,
            "http://mcp-server:9100",
            id="docker-sse",
        ),
    ],
)
def test_mcp_instance_endpoint_url_matches_runtime_transport(config, port, expected):
    instance = McpInstance(
        server_name="test",
        server_config=config,
        kwargs={},
        instance_id="test",
        container_name="mcp-server",
        project_root=Path("project"),
        port=port,
    )

    assert instance.endpoint_url == expected
