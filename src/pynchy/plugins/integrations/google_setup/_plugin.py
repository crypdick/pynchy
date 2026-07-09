"""Built-in Google Setup plugin — MCP specs + service handlers."""

from __future__ import annotations

from typing import Any

import pluggy

from pynchy.plugins.integrations.google_setup._handler import handle_setup_google

hookimpl = pluggy.HookimplMarker("pynchy")


class GoogleMcpPlugin:
    """Base MCP specs for Google services (gdrive, gcal).

    These are templates — they exist only to be inherited by config
    instances (e.g., ``[mcp_servers.gdrive.mycompany]``).  If no instances
    are declared, the template sits idle.
    """

    @hookimpl
    def pynchy_mcp_server_spec(self) -> list[dict[str, Any]]:
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
            async def _handler(data: dict[str, Any], _profile: str = profile) -> dict[str, Any]:
                data["chrome_profile"] = _profile
                return await handle_setup_google(data)

            tools[f"setup_google_{profile}"] = _handler

        return {"tools": tools}
