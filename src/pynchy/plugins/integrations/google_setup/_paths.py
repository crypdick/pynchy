"""Constants, paths, and scope computation for Google Setup.

Chrome-profile-aware paths for OAuth client credentials and tokens, plus the
scope registry mapping MCP server template names to OAuth scopes + API IDs.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.

import pynchy.config as config
from pynchy.config.settings import (  # noqa: TC001, RUF100 - beartype resolves this runtime annotation.
    Settings,
)
from pynchy.plugins.integrations.browser import project_root

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GCP_CONSOLE = "https://console.cloud.google.com"
OAUTH_CALLBACK_PORT = 8085
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_ENDPOINT_URL = "https://oauth2.googleapis.com/token"
DEFAULT_PROJECT_ID = "pynchy-gdrive"

# ---------------------------------------------------------------------------
# Scope registry — maps MCP server template names to OAuth scopes + API IDs
# ---------------------------------------------------------------------------

SERVER_SCOPES: dict[str, tuple[list[str], str]] = {
    "gdrive": (
        ["https://www.googleapis.com/auth/drive.readonly"],
        "drive.googleapis.com",
    ),
    "gcal": (
        ["https://www.googleapis.com/auth/calendar"],
        "calendar-json.googleapis.com",
    ),
}

# Service management scope is always included (enables REST API enablement)
SERVICE_MANAGEMENT_SCOPE = "https://www.googleapis.com/auth/service.management"

SERVICE_USAGE_URL = "https://serviceusage.googleapis.com/v1"


# ---------------------------------------------------------------------------
# Paths — chrome-profile-aware
# ---------------------------------------------------------------------------


def chrome_profile_dir(profile_name: str) -> Path:
    """Host directory for a chrome profile's auth artifacts."""
    d = project_root() / "data" / "chrome-profiles" / profile_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def keys_path(profile_name: str) -> Path:
    """OAuth client credentials (gcp-oauth.keys.json) for a chrome profile."""
    return chrome_profile_dir(profile_name) / "gcp-oauth.keys.json"


def credentials_path(profile_name: str) -> Path:
    """OAuth tokens (credentials.json) for a chrome profile."""
    return chrome_profile_dir(profile_name) / "credentials.json"


def download_dir() -> Path:
    """Temporary download directory for credential files."""
    d = project_root() / "data" / "tmp" / "google-setup"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Scope computation — union scopes from all services referencing a profile
# ---------------------------------------------------------------------------


def compute_scopes_for_profile(profile_name: str) -> tuple[str, list[str]]:
    """Compute the union of OAuth scopes and API IDs for a chrome profile.

    Checks which resolved MCP tool names reference this profile across all
    workspaces. Returns (space-separated scopes, sorted API IDs).
    """
    settings = config.get_settings()
    scopes: set[str] = set()
    apis: set[str] = set()

    for svc, (svc_scopes, api_id) in SERVER_SCOPES.items():
        instance_name = f"{svc}.{profile_name}"
        for workspace_name in settings.workspaces:
            if _workspace_selects_mcp_tool(settings, workspace_name, instance_name):
                scopes.update(svc_scopes)
                apis.add(api_id)
                break

    # Always include service management scope for REST API enablement
    scopes.add(SERVICE_MANAGEMENT_SCOPE)

    return " ".join(sorted(scopes)), sorted(apis)


def workspace_chrome_profiles(source_group: str) -> set[str]:
    """Return the chrome profiles selected by a workspace's MCP tools."""
    s = config.get_settings()
    resolved = s.resolved_workspace_config(source_group)
    if not resolved:
        return set()

    profiles: set[str] = set()
    for entry in resolved.tools:
        tool = s.tools.get(entry)
        if tool is None or tool.type != "mcp" or "." not in entry:
            continue
        _, inst_name = entry.split(".", 1)
        if inst_name in s.chrome_profiles:
            profiles.add(inst_name)
    return profiles


def _workspace_selects_mcp_tool(settings: Settings, workspace_name: str, tool_name: str) -> bool:
    resolved = settings.resolved_workspace_config(workspace_name)
    if resolved is None or tool_name not in resolved.tools:
        return False
    tool = settings.tools.get(tool_name)
    return tool is not None and tool.type == "mcp"
