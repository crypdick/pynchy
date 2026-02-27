"""Playwright browser plugin — general-purpose browser control for agents.

Provides playwright-mcp as a script-type MCP server, plus a browser-control
skill that teaches agents how to use browser tools effectively.

Security: trust defaults mark this as a public_source (untrusted web content).
The MCP proxy applies content fencing and Cop inspection automatically.
"""

from __future__ import annotations

from pathlib import Path

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")

_BROWSER_MCP_PORT = 9100


class PlaywrightBrowserPlugin:
    """Playwright browser plugin — wraps playwright-mcp for agent browser control."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict:
        """Register playwright-mcp as a script-type MCP server."""
        return {
            "name": "browser",
            "command": "npx",
            "args": ["@playwright/mcp@latest"],
            "port": _BROWSER_MCP_PORT,
            "transport": "streamable_http",
            "idle_timeout": 300,
            "trust": {
                "public_source": True,
                "secret_data": False,
                "public_sink": False,
                "dangerous_writes": False,
            },
        }

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        """Contribute the browser-control skill."""
        # __file__ is src/pynchy/integrations/plugins/playwright_browser.py
        # Project root is 5 levels up: plugins/ -> integrations/ -> pynchy/ -> src/ -> root
        skill_dir = (
            Path(__file__).resolve().parent.parent.parent.parent.parent
            / "container"
            / "skills"
            / "browser-control"
        )
        if skill_dir.is_dir():
            return [str(skill_dir)]
        return []
