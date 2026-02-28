"""Google setup tools — one per chrome profile, auto-generated.

Reads PYNCHY_CHROME_PROFILES env var (comma-separated, injected by the
host based on which profiles the workspace's MCP servers reference) and
registers a ``setup_google_{profile}`` IPC tool for each one.

The agent sees tools like ``setup_google_mycompany`` — same naming pattern
as the MCP tools (``mcp__gdrive_mycompany__search``).  No guessing which
profiles are available.
"""

import os

from agent_runner.agent_tools._registry import register_ipc_tool

_raw = os.environ.get("PYNCHY_CHROME_PROFILES", "")
_profiles = [p.strip() for p in _raw.split(",") if p.strip()]

for _profile in _profiles:
    register_ipc_tool(
        name=f"setup_google_{_profile}",
        description=(
            f"Set up Google services (Drive, Calendar, etc.) for the "
            f"'{_profile}' chrome profile. Idempotent — checks state and "
            f"only does what's missing: GCP project creation, API enablement, "
            f"OAuth consent screen, credential creation, and OAuth token "
            f"exchange. Opens a browser on the host (visible via noVNC on "
            f"headless servers) for Google login and OAuth consent. Required "
            f"scopes are auto-computed from which MCP servers reference "
            f"this profile."
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
