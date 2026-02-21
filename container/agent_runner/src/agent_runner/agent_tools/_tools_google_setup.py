"""Google Drive setup tools — GCP project setup, API enablement, and OAuth via IPC.

Uses Playwright on the host side with a persistent browser context and
noVNC for human interaction (Google login, OAuth consent).  Three tools
of increasing scope:

- ``enable_gdrive_api`` — just enable the Drive API (fixes 403 errors)
- ``authorize_gdrive`` — re-run OAuth token exchange (existing credentials)
- ``setup_gdrive`` — full flow from scratch (project + API + consent + OAuth)

These tools write IPC requests that the host processes after applying
policy middleware.
"""

from agent_runner.agent_tools._registry import register_ipc_tool

register_ipc_tool(
    name="enable_gdrive_api",
    description=(
        "Enable the Google Drive API for a GCP project. Use this when GDrive "
        "MCP tools return 403 errors indicating the API hasn't been enabled. "
        "Opens a browser on the host (visible via noVNC) to automate the GCP "
        "Console. May require human interaction for Google login."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": (
                    "GCP project ID. Auto-detected from existing credentials if not provided."
                ),
            },
        },
    },
)

register_ipc_tool(
    name="authorize_gdrive",
    description=(
        "Run the Google OAuth token exchange flow using existing OAuth client "
        "credentials (data/gcp-oauth.keys.json). Opens a browser for the user "
        "to click 'Allow' on the Google consent screen (via noVNC on headless "
        "servers). Saves the resulting tokens to the mcp-gdrive Docker volume. "
        "Use this when tokens have expired but credentials already exist."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "keys_path": {
                "type": "string",
                "description": (
                    "Path to the OAuth client JSON file (default: data/gcp-oauth.keys.json)"
                ),
            },
        },
    },
)

register_ipc_tool(
    name="setup_gdrive",
    description=(
        "Full Google Drive setup: create GCP project, enable Drive API, "
        "configure OAuth consent screen, create Desktop App credentials, "
        "and run OAuth authorization. Opens a browser on the host (visible "
        "via noVNC) for human interaction. Only needed for first-time setup "
        "or if the GCP project was deleted."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": (
                    "GCP project ID to create or reuse. Auto-detected from "
                    "existing credentials if not provided."
                ),
            },
        },
    },
)
