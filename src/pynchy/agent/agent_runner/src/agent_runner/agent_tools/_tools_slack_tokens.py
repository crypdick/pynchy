"""Slack token tools — extract and refresh browser tokens via IPC service requests.

Uses Playwright persistent browser contexts on the host side. After one
manual login (human handles CAPTCHA/magic-link), subsequent token extractions
run headlessly using the saved session.

These tools write IPC requests that the host processes after applying
policy middleware.
"""

from agent_runner.agent_tools._registry import register_ipc_tool

register_ipc_tool(
    name="refresh_slack_tokens",
    description=(
        "Extract fresh Slack browser tokens from a persistent browser session. "
        "Requires a prior setup_slack_session call to establish the browser "
        "session via manual login. Once set up, this tool runs headlessly to "
        "extract fresh tokens whenever the old ones expire."
    ),
    input_schema={
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

register_ipc_tool(
    name="setup_slack_session",
    description=(
        "Launch a headed browser for manual Slack login. Saves the session "
        "to a persistent profile for future headless use by refresh_slack_tokens. "
        "On headless servers, automatically starts a virtual display with noVNC "
        "web access on port 6080."
    ),
    input_schema={
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
