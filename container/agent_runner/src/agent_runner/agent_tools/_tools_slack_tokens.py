"""Slack token tools — extract and refresh browser tokens via IPC service requests.

Uses Playwright persistent browser contexts on the host side. After one
manual login (human handles CAPTCHA/magic-link), subsequent token extractions
run headlessly using the saved session.

These tools write IPC requests that the host processes after applying
policy middleware.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from agent_runner.agent_tools._ipc_request import ipc_service_request
from agent_runner.agent_tools._registry import ToolEntry, register

# --- refresh_slack_tokens ---


def _refresh_slack_tokens_definition() -> Tool:
    return Tool(
        name="refresh_slack_tokens",
        description=(
            "Extract fresh Slack browser tokens from a persistent browser session. "
            "Requires a prior setup_slack_session call to establish the browser "
            "session via manual login. Once set up, this tool runs headlessly to "
            "extract fresh tokens whenever the old ones expire."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_name": {
                    "type": "string",
                    "description": (
                        'Identifier for the browser profile (e.g., "acme"). '
                        "Must match the name used during setup_slack_session."
                    ),
                },
                "xoxc_var": {
                    "type": "string",
                    "description": (
                        'Env var name to write the new xoxc token to (e.g., "SLACK_XOXC_ACME")'
                    ),
                },
                "xoxd_var": {
                    "type": "string",
                    "description": (
                        'Env var name to write the new xoxd token to (e.g., "SLACK_XOXD_ACME")'
                    ),
                },
                "workspace_url": {
                    "type": "string",
                    "description": "Slack workspace URL (default: https://app.slack.com)",
                    "default": "https://app.slack.com",
                },
            },
            "required": ["workspace_name", "xoxc_var", "xoxd_var"],
        },
    )


async def _refresh_slack_tokens_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "refresh_slack_tokens",
        {
            "workspace_name": arguments["workspace_name"],
            "xoxc_var": arguments["xoxc_var"],
            "xoxd_var": arguments["xoxd_var"],
            "workspace_url": arguments.get("workspace_url", "https://app.slack.com"),
        },
    )


# --- setup_slack_session ---


def _setup_slack_session_definition() -> Tool:
    return Tool(
        name="setup_slack_session",
        description=(
            "Launch a headed browser for manual Slack login. Saves the session "
            "to a persistent profile for future headless use by refresh_slack_tokens. "
            "On headless servers, automatically starts a virtual display with noVNC "
            "web access on port 6080."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_name": {
                    "type": "string",
                    "description": (
                        'Identifier for the browser profile (e.g., "acme"). '
                        "Used by refresh_slack_tokens to find this session later."
                    ),
                },
                "workspace_url": {
                    "type": "string",
                    "description": "Slack workspace URL (default: https://app.slack.com)",
                    "default": "https://app.slack.com",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "How long to wait for login completion (default: 120s)",
                    "default": 120,
                },
            },
            "required": ["workspace_name"],
        },
    )


async def _setup_slack_session_handle(arguments: dict) -> list[TextContent]:
    return await ipc_service_request(
        "setup_slack_session",
        {
            "workspace_name": arguments["workspace_name"],
            "workspace_url": arguments.get("workspace_url", "https://app.slack.com"),
            "timeout_seconds": arguments.get("timeout_seconds", 120),
        },
    )


register(
    "refresh_slack_tokens",
    ToolEntry(definition=_refresh_slack_tokens_definition, handler=_refresh_slack_tokens_handle),
)
register(
    "setup_slack_session",
    ToolEntry(definition=_setup_slack_session_definition, handler=_setup_slack_session_handle),
)
