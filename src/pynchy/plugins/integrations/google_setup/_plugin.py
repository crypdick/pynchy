"""Built-in Google Setup plugin — MCP specs + service handlers."""

from __future__ import annotations

import hashlib
from typing import Any

import pluggy

from pynchy.actions.api import ActionId
from pynchy.plugins.api import (
    ApprovalContract,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityRequirement,
    CapabilityRequirementKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    McpServerConfig,
    McpServerSpec,
)
from pynchy.plugins.integrations.google_setup._handler import handle_setup_google

hookimpl = pluggy.HookimplMarker("pynchy")


class GoogleMcpPlugin:
    """Base MCP specs for Google services (gdrive, gcal).

    These are templates — they exist only to be inherited by config
    instances (e.g., ``[mcp_servers.gdrive.mycompany]``).  If no instances
    are declared, the template sits idle.
    """

    @hookimpl
    def pynchy_mcp_server_spec(self) -> tuple[McpServerSpec, ...]:
        return (
            McpServerSpec(
                name="gdrive",
                config=McpServerConfig(
                    type="docker",
                    image="pynchy-mcp-gdrive:latest",
                    dockerfile="src/pynchy/agent/mcp/gdrive.Dockerfile",
                    build_context="src/pynchy/agent/mcp",
                    port=3100,
                    transport="streamable_http",
                    env={"GDRIVE_OAUTH_PATH": "/home/chrome/gcp-oauth.keys.json"},
                ),
            ),
            McpServerSpec(
                name="gcal",
                config=McpServerConfig(
                    type="docker",
                    image="pynchy-mcp-gcal:latest",
                    dockerfile="src/pynchy/agent/mcp/gcal.Dockerfile",
                    build_context="src/pynchy/agent/mcp",
                    port=3200,
                    transport="streamable_http",
                ),
            ),
        )


class GoogleSetupPlugin:
    """Host-side handlers for Google OAuth setup.

    Registers one ``setup_google_{profile}`` handler per chrome profile
    defined in layered settings. Each handler is a closure that injects the
    profile name into the request data before calling the shared handler.
    """

    def __init__(self, profiles: tuple[str, ...] = ()) -> None:
        self._profiles = profiles

    def configure(self, profiles: tuple[str, ...]) -> None:
        """Apply the configured browser profiles before actions are registered."""
        self._profiles = profiles

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return HostActionRegistration(
            actions=tuple(_profile_setup_action(profile) for profile in self._profiles)
        )


def _profile_setup_action(profile: str) -> HostActionDescriptor:
    tool_name = f"setup_google_{profile}"
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=_profile_capability_id(profile),
            kind=CapabilityKind.HOST_ACTION,
            owner="google-setup",
            summary=f"Set up Google services for browser profile {profile!r}.",
            action_ids=(ActionId("integration.google.profile.setup"),),
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.CONFIG,
                    name=f"chrome_profiles.{profile}",
                    description=f"Declare the {profile!r} Chrome profile in host configuration.",
                ),
            ),
            documentation="docs/integrations/google/index.md",
        ),
        tool_name=HostToolName(tool_name),
        handler=_profile_handler(profile),
        access=HostActionAccess.WRITE,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(IdempotencyMode.IPC_REQUEST_ID),
        audit=AuditContract(),
    )


def _profile_handler(profile: str) -> HostActionHandler:
    async def handle(data: dict[str, Any]) -> dict[str, Any]:
        data["chrome_profile"] = profile
        return await handle_setup_google(data)

    return handle


def _profile_capability_id(profile: str) -> CapabilityId:
    # Profile names can contain characters that capability IDs reject. A hash
    # keeps each configured profile stable and collision-resistant without
    # weakening the capability ID grammar.
    digest = hashlib.sha256(profile.encode()).hexdigest()[:12]
    return CapabilityId(f"integration.google.profile.setup.profile-{digest}")
