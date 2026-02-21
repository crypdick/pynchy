"""Built-in Slack token extractor plugin.

Registers a script-type MCP server that automates extraction of Slack browser
tokens (``xoxc``/``xoxd``) via headless Playwright. The MCP server is a
standalone uv script with inline dependencies — heavy deps (Playwright, FastMCP)
never touch pynchy's virtualenv.

The agent calls the ``refresh_slack_tokens`` tool when existing tokens expire.
"""

from __future__ import annotations

from pathlib import Path

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")

# Standalone uv script with PEP 723 inline deps — not a package module.
_SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "extract_slack_token.py"


class SlackTokenExtractorPlugin:
    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict:
        return {
            "name": "slack_token_extractor",
            "command": "uv",
            "args": ["run", str(_SCRIPT)],
            "port": 8457,
            "transport": "streamable_http",
            "idle_timeout": 300,
        }
