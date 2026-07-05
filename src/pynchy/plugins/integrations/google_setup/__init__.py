"""Built-in Google Setup plugin — MCP specs + service handlers.

Two plugin classes:

**GoogleMcpPlugin** — provides base MCP server specs for ``gdrive`` and
``gcal``.  These are templates: they exist only to be inherited by config
instances (e.g., ``[mcp_servers.gdrive.mycompany]``).

**GoogleSetupPlugin** — provides host-side handlers for GCP project setup,
API enablement, OAuth consent screen configuration, and OAuth token
exchange.  Single idempotent tool: ``setup_google(chrome_profile=...)``.

Uses the system Chrome/Chromium binary (``chrome_path()``) — Playwright's
vendored Chromium is never used (see ``integrations.browser`` for rationale).
On headless servers, auto-starts Xvfb + noVNC so the user can interact
via web browser for Google login and OAuth consent.

Each GCP Console step attempts Playwright automation first and falls back
to printed instructions + noVNC if selectors fail (Google changes their
UI often).

Implementation is split across this package:

- ``_paths``: constants, chrome-profile-aware paths, scope computation
- ``_rest_api``: Service Usage REST API helpers (avoids browser automation
  when credentials are already valid)
- ``_console``: Playwright automation against the GCP Console
- ``_oauth``: OAuth token exchange and the authorization-code flow
- ``_handler``: the idempotent ``setup_google`` orchestration handler
"""

from __future__ import annotations

from typing import Any

import pluggy

from pynchy.plugins.integrations.google_setup._handler import handle_setup_google

hookimpl = pluggy.HookimplMarker("pynchy")


class GoogleMcpPlugin:
    """Base MCP specs for Google services (gdrive, gcal).

    These are templates — they exist only to be inherited by config
    instances (e.g., ``[mcp_servers.gdrive.mycompany]``).  If no instances
    are declared, the template sits unused.
    """

    @hookimpl
    def pynchy_mcp_server_spec(self) -> list[dict]:
        return [
            {
                "name": "gdrive",
                "type": "docker",
                "image": "pynchy-mcp-gdrive:latest",
                "dockerfile": "src/pynchy/agent/mcp/gdrive.Dockerfile",
                "port": 3100,
                "transport": "streamable_http",
                "env": {"GDRIVE_OAUTH_PATH": "/home/chrome/gcp-oauth.keys.json"},
            },
            {
                "name": "gcal",
                "type": "docker",
                "image": "pynchy-mcp-gcal:latest",
                "dockerfile": "src/pynchy/agent/mcp/gcal.Dockerfile",
                "port": 3200,
                "transport": "streamable_http",
            },
        ]


class GoogleSetupPlugin:
    """Host-side handlers for Google OAuth setup.

    Registers one ``setup_google_{profile}`` handler per chrome profile
    defined in config.toml.  Each handler is a closure that injects the
    profile name into the request data before calling the shared handler.
    """

    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        from pynchy.config import get_settings

        tools: dict[str, Any] = {}
        for profile in get_settings().chrome_profiles:
            # Closure captures profile by value via default arg
            async def _handler(data: dict, _profile: str = profile) -> dict:
                data["chrome_profile"] = _profile
                return await handle_setup_google(data)

            tools[f"setup_google_{profile}"] = _handler

        return {"tools": tools}
