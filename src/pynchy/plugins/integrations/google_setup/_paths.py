"""Constants, paths, and scope computation for Google Setup.

Chrome-profile-aware paths for OAuth client credentials and tokens, plus the
scope registry mapping MCP server template names to OAuth scopes + API IDs.
"""

from __future__ import annotations

from collections.abc import (
    Callable,  # noqa: TC003 - beartype resolves Google setup runtime callbacks at runtime.
)
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - beartype resolves this runtime annotation.

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


@dataclass(frozen=True)
class GoogleSetupRuntime:
    """Resolved host paths and workspace policy for Google setup."""

    data_dir: Path
    chrome_profiles: frozenset[str]
    workspace_names: tuple[str, ...]
    workspace_tools: Callable[[str], tuple[str, ...] | None]
    workspace_is_admin: Callable[[str], bool]
    mcp_tool_names: frozenset[str]


_runtime: GoogleSetupRuntime | None = None


def configure_google_setup_runtime(runtime: GoogleSetupRuntime) -> None:
    """Set Google setup configuration before host actions run."""
    global _runtime  # noqa: PLW0603 - one host process owns this configured runtime.
    _runtime = runtime


def google_setup_runtime() -> GoogleSetupRuntime:
    """Return the resolved Google setup runtime."""
    if _runtime is None:
        raise RuntimeError("Google setup runtime has not been configured")
    return _runtime


# ---------------------------------------------------------------------------
# Paths — chrome-profile-aware
# ---------------------------------------------------------------------------


def chrome_profile_dir(profile_name: str) -> Path:
    """Host directory for a chrome profile's auth artifacts."""
    d = google_setup_runtime().data_dir / "chrome-profiles" / profile_name
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
    d = google_setup_runtime().data_dir / "tmp" / "google-setup"
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
    runtime = google_setup_runtime()
    scopes: set[str] = set()
    apis: set[str] = set()

    for svc, (svc_scopes, api_id) in SERVER_SCOPES.items():
        instance_name = f"{svc}.{profile_name}"
        for workspace_name in runtime.workspace_names:
            if _workspace_selects_mcp_tool(runtime, workspace_name, instance_name):
                scopes.update(svc_scopes)
                apis.add(api_id)
                break

    # Always include service management scope for REST API enablement
    scopes.add(SERVICE_MANAGEMENT_SCOPE)

    return " ".join(sorted(scopes)), sorted(apis)


def workspace_chrome_profiles(source_group: str) -> set[str]:
    """Return the chrome profiles selected by a workspace's MCP tools."""
    runtime = google_setup_runtime()
    selected_tools = runtime.workspace_tools(source_group)
    if selected_tools is None:
        return set()

    profiles: set[str] = set()
    for entry in selected_tools:
        if entry not in runtime.mcp_tool_names or "." not in entry:
            continue
        _, inst_name = entry.split(".", 1)
        if inst_name in runtime.chrome_profiles:
            profiles.add(inst_name)
    return profiles


def _workspace_selects_mcp_tool(
    runtime: GoogleSetupRuntime, workspace_name: str, tool_name: str
) -> bool:
    selected_tools = runtime.workspace_tools(workspace_name)
    return (
        selected_tools is not None
        and tool_name in selected_tools
        and tool_name in runtime.mcp_tool_names
    )
