"""Factories that keep built-in action declarations concise and consistent."""

from __future__ import annotations

from pynchy._action_contract import (
    ActionId,
    ActionSpec,
    ActionSurface,
    ActionTransport,
    EvidenceRequirement,
)


def build_action(
    action_id: str,
    owner: str,
    summary: str,
    surface: ActionSurface,
    *,
    canary: str | None = None,
) -> ActionSpec:
    """Declare one action with its externally visible Pynchy surface."""
    return ActionSpec(
        ActionId(action_id),
        owner,
        summary,
        (
            EvidenceRequirement.HERMETIC_AND_AGENTIC
            if canary is not None
            else EvidenceRequirement.HERMETIC
        ),
        canary,
        (surface,),
    )


def agent_action(
    action_id: str,
    owner: str,
    summary: str,
    tool_name: str,
    *,
    canary: str | None = None,
) -> ActionSpec:
    """Declare an action exposed through Pynchy's built-in agent MCP server."""
    return build_action(
        action_id,
        owner,
        summary,
        ActionSurface(ActionTransport.AGENT_TOOL, tool_name),
        canary=canary,
    )


def mcp_action(
    action_id: str,
    owner: str,
    summary: str,
    tool_name: str,
    *,
    canary: str | None = None,
) -> ActionSpec:
    """Declare an action exposed by a first-party integration MCP server."""
    return build_action(
        action_id,
        owner,
        summary,
        ActionSurface(ActionTransport.MCP_TOOL, tool_name),
        canary=canary,
    )
