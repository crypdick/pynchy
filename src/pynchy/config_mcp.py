"""MCP server configuration models.

Defines the Pydantic models for MCP server definitions, groups, and presets.
Imported by :mod:`pynchy.config` to keep that file lean.

Example TOML::

    [mcp_servers.playwright]
    type = "docker"
    image = "mcp/playwright:latest"
    args = ["--headless", "--port", "8931", "--host", "0.0.0.0", "--allowed-hosts", "*"]
    port = 8931
    transport = "http"
    idle_timeout = 600

    [mcp_servers.some-remote-api]
    type = "url"
    url = "https://api.example.com/mcp"
    transport = "streamable_http"
    auth_value_env = "SOME_API_KEY"
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator


class McpServerConfig(BaseModel):
    """Global MCP server definition."""

    type: Literal["docker", "url"]

    # Docker fields
    image: str | None = None
    args: list[str] = []
    port: int | None = None
    idle_timeout: int = 600  # seconds; 0 = never stop

    # URL fields
    url: str | None = None

    # Common
    # "http" = Streamable HTTP (preferred for Docker — no persistent connection).
    # LiteLLM accepts "sse", "http", "stdio".
    transport: Literal["sse", "http", "streamable_http"] = "sse"
    auth_value_env: str | None = None  # env var name for auth token (never inline secrets)

    @model_validator(mode="after")
    def _validate_type_fields(self) -> McpServerConfig:
        if self.type == "docker":
            if not self.image:
                raise ValueError("Docker MCP servers require 'image'")
            if self.port is None:
                raise ValueError("Docker MCP servers require 'port'")
        elif self.type == "url":
            if not self.url:
                raise ValueError("URL MCP servers require 'url'")
        return self
