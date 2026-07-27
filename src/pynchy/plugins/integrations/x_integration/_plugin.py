"""Built-in X (Twitter) integration plugin (service handler)."""

from __future__ import annotations

from pathlib import Path

import pluggy

from pynchy.actions import ActionId
from pynchy.capabilities import (
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
)
from pynchy.plugins.integrations.browser import check_browser_plugin_deps
from pynchy.plugins.integrations.x_integration._actions import (
    handle_setup_x_session,
    handle_x_like,
    handle_x_post,
    handle_x_quote,
    handle_x_reply,
    handle_x_retweet,
)

hookimpl = pluggy.HookimplMarker("pynchy")
type _ActionDefinition = tuple[str, str, str, HostActionHandler]

# Validate browser deps at import time so failures surface on plugin load
check_browser_plugin_deps("setup_x_session")


_X_ACTIONS: tuple[_ActionDefinition, ...] = (
    (
        "setup_x_session",
        "social.x.session.setup",
        "Create a persistent X browser session through interactive login.",
        handle_setup_x_session,
    ),
    ("x_post", "social.x.post", "Publish a post to X.", handle_x_post),
    ("x_like", "social.x.like", "Like a post on X.", handle_x_like),
    ("x_reply", "social.x.reply", "Reply to a post on X.", handle_x_reply),
    ("x_retweet", "social.x.repost", "Repost a post on X.", handle_x_retweet),
    ("x_quote", "social.x.quote", "Quote a post on X.", handle_x_quote),
)


def _x_action(definition: _ActionDefinition) -> HostActionDescriptor:
    tool_name, action_id, summary, handler = definition
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(action_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="x-integration",
            summary=summary,
            action_ids=(ActionId(action_id),),
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                    name="x_integration",
                    description="Enable the X integration for this workspace.",
                ),
            ),
            documentation="docs/integrations/x-integration.md",
        ),
        tool_name=HostToolName(tool_name),
        handler=handler,
        access=HostActionAccess.WRITE,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(IdempotencyMode.IPC_REQUEST_ID),
        audit=AuditContract(),
        policy_service="x_integration",
    )


X_HOST_ACTIONS = HostActionRegistration(actions=tuple(_x_action(action) for action in _X_ACTIONS))


class XIntegrationPlugin:
    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return X_HOST_ACTIONS

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        skill_dir = Path(__file__).resolve().parent / "skills" / "x-integration"
        if skill_dir.is_dir():
            return [str(skill_dir)]
        return []
