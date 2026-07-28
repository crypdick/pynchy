"""Playwright browser plugin — general-purpose browser control for agents.

Provides playwright-mcp as a script-type MCP server, plus a browser-control
skill that teaches agents how to use browser tools effectively.

Security: trust defaults mark this as a public_source (untrusted web content).
The MCP proxy applies content fencing and Cop inspection automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

import pluggy

from pynchy.plugins.api import McpServerConfig, McpServerSpec
from pynchy.types import ServiceTrustConfig

hookimpl = pluggy.HookimplMarker("pynchy")

_BROWSER_MCP_PORT = 9100
_BROWSER_MCP_HOST = "localhost"
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _browser_mcp_args() -> list[str]:
    args = ["@playwright/mcp@latest"]
    if os.environ.get("PYNCHY_BROWSER_HEADLESS", "").strip().lower() in _TRUE_ENV_VALUES:
        args.append("--headless")
    args.extend(
        [
            "--port",
            "{port}",
            "--host",
            _BROWSER_MCP_HOST,
        ]
    )
    return args


class PlaywrightBrowserPlugin:
    """Playwright browser plugin — wraps playwright-mcp for agent browser control."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> tuple[McpServerSpec, ...]:
        """Register playwright-mcp as a script-type MCP server.

        The ``{port}`` placeholder is expanded at launch time to each
        instance's assigned port (via ``mcp_server_instances`` or
        ``_resolve_all_instances``).  This lets multiple workspaces
        each run their own Playwright process without port conflicts.
        """
        return (
            McpServerSpec(
                name="browser",
                config=McpServerConfig(
                    type="script",
                    command="npx",
                    args=_browser_mcp_args(),
                    port=_BROWSER_MCP_PORT,
                    transport="streamable_http",
                    idle_timeout=300,
                ),
                trust=ServiceTrustConfig(
                    public_source=True,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            ),
        )

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        """Contribute the browser-control skill."""
        skill_dir = Path(__file__).resolve().parent / "skills" / "browser-control"
        if skill_dir.is_dir():
            return [str(skill_dir)]
        return []
