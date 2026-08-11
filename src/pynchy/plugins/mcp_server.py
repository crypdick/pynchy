"""Validated MCP server templates supplied by plugins or user tool settings.

Defines the Pydantic models for MCP server definitions, groups, and presets.
The template is part of the plugin extension contract; configuration sources
only parse values into it.

Example TOML::

    [tools.playwright]
    type = "mcp"

    [tools.playwright.mcp]
    runtime = "docker"
    image = "mcp/playwright:latest"
    args = ["--headless", "--port", "8931", "--host", "0.0.0.0", "--allowed-hosts", "*"]
    port = 8931
    transport = "http"
    idle_timeout = 600

    [tools.slack_mcp_acme]
    type = "mcp"
    required_env = ["SLACK_MCP_XOXC_TOKEN", "SLACK_MCP_XOXD_TOKEN"]

    [tools.slack_mcp_acme.mcp]
    runtime = "docker"
    image = "ghcr.io/korotovsky/slack-mcp-server:latest"
    port = 8080
    transport = "http"
    env = { SLACK_MCP_HOST = "0.0.0.0", SLACK_MCP_PORT = "8080" }

    [tools.some-remote-api]
    type = "mcp"

    [tools.some-remote-api.mcp]
    runtime = "url"
    url = "https://api.example.com/mcp"
    transport = "streamable_http"
    auth_value_env = "SOME_API_KEY"

    # Host script MCP (HTTP subprocess managed by Pynchy):
    [tools.my_custom_tool]
    type = "mcp"

    [tools.my_custom_tool.mcp]
    runtime = "script"
    command = "uv"
    args = ["run", "scripts/my-tool.py"]
    port = 8080
    transport = "streamable_http"

    # Host stdio MCP (wrapped in a loopback Streamable HTTP bridge):
    [tools.my_stdio_tool]
    type = "mcp"

    [tools.my_stdio_tool.mcp]
    runtime = "stdio"
    command = "npx"
    args = ["-y", "some-stdio-mcp"]
    port = 8081
    transport = "streamable_http"
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

_DOCKER_IMAGE_REQUIRED = "Docker MCP servers require 'image'"
_DOCKER_PORT_REQUIRED = "Docker MCP servers require 'port'"
_SCRIPT_COMMAND_REQUIRED = "Script MCP servers require 'command'"
_SCRIPT_PORT_REQUIRED = "Script MCP servers require 'port'"
_STDIO_COMMAND_REQUIRED = "Stdio MCP servers require 'command'"
_STDIO_PORT_REQUIRED = "Stdio MCP servers require 'port'"
_STDIO_TRANSPORT_REQUIRED = "Stdio MCP servers require HTTP transport"
_URL_REQUIRED = "URL MCP servers require 'url'"


class McpServerConfig(BaseModel):
    """Global MCP server definition."""

    model_config = {"extra": "forbid"}

    type: Literal["docker", "url", "script", "stdio"]

    # Docker fields
    image: str | None = None
    # Relative path from project root to a local Dockerfile (e.g.,
    # "src/pynchy/agent/mcp/notebook.Dockerfile"). When set, the MCP manager builds
    # the image locally instead of pulling from a registry.
    dockerfile: str | None = None
    # Relative Docker build context. Defaults to the project root.
    build_context: str = "."
    # Additional ports to publish beyond the primary MCP port (e.g., JupyterLab on 8888).
    extra_ports: list[int] = []

    # Host-process fields
    command: str | None = None  # executable to run (e.g., "uv") — required for script/stdio

    # Shared by Docker and host-process server configs.
    args: list[str] = []
    port: int | None = None
    idle_timeout: int = 600  # seconds; 0 = never stop
    # Bound on-demand readiness waits. A failed optional tool must not hold up
    # the agent launch behind the global container health-check timeout.
    startup_timeout_seconds: float = 5.0
    env: dict[str, str] = {}  # static, non-secret runtime configuration
    # Volume mounts passed as -v flags. Each entry is "host_path:container_path".
    # Relative host paths are resolved from project_root.
    volumes: list[str] = []

    # When True, mcp_manager auto-injects workspace=<group_folder> into kwargs.
    # This gives each workspace a separate server instance with workspace-scoped
    # args, without requiring per-workspace config in pynchy.toml.
    inject_workspace: bool = False

    @field_validator("startup_timeout_seconds")
    @classmethod
    def _validate_startup_timeout(cls, value: float) -> float:
        """Reject non-positive readiness deadlines at config load time."""
        if value <= 0:
            msg = "MCP startup_timeout_seconds must be greater than zero"
            raise ValueError(msg)
        return value

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
                raise ValueError(_DOCKER_IMAGE_REQUIRED)
            if self.port is None:
                raise ValueError(_DOCKER_PORT_REQUIRED)
        elif self.type == "url":
            if not self.url:
                raise ValueError(_URL_REQUIRED)
        elif self.type == "script":
            if not self.command:
                raise ValueError(_SCRIPT_COMMAND_REQUIRED)
            if self.port is None:
                raise ValueError(_SCRIPT_PORT_REQUIRED)
        else:
            if not self.command:
                raise ValueError(_STDIO_COMMAND_REQUIRED)
            if self.port is None:
                raise ValueError(_STDIO_PORT_REQUIRED)
            if self.transport not in ("http", "streamable_http"):
                raise ValueError(_STDIO_TRANSPORT_REQUIRED)
        return self
