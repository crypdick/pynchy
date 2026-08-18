"""Tests for operator-authored permission policy configuration."""

from unittest.mock import patch

import pytest
from conftest import make_settings
from pydantic import ValidationError

from pynchy.config.api import PermissionConfig, ProfileConfig, WorkspaceConfig
from pynchy.config.models import RouteConfig
from pynchy.config.workspace_layout import WorkspaceScopeConfig, WorkspaceThreadConfig
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspacePolicy,
    clear_runtime_workspace_policies,
    load_resolved_config,
    register_runtime_workspace_policy,
)
from pynchy.workspace.api import CapabilityRule


def test_permission_buckets_map_to_runtime_decisions():
    permissions = PermissionConfig(
        allow=["desktop.computer.use"],
        ask=["mcp.email.send"],
        deny=["mcp.email.delete"],
    )

    assert permissions.decisions == {
        "desktop.computer.use": "allow",
        "mcp.email.send": "needs_human",
        "mcp.email.delete": "deny",
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"allow": ["mcp.email.send", "mcp.email.send"]},
        {"allow": ["mcp.email.send"], "ask": ["mcp.email.send"]},
    ],
)
def test_duplicate_permissions_are_rejected(raw):
    with pytest.raises(ValidationError, match=r"permission.*more than once"):
        PermissionConfig.model_validate(raw)


def test_empty_permission_pattern_is_rejected():
    with pytest.raises(ValidationError, match="must not be empty"):
        PermissionConfig(allow=[" "])


def test_legacy_capability_syntax_is_rejected():
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProfileConfig(capabilities={"mcp.email.send": {"decision": "allow"}})


def test_new_permission_syntax_resolves_for_profiles_and_routes():
    profile = ProfileConfig(permissions={"allow": ["mcp.email.send"]})
    route = RouteConfig(
        source="connection.matrix.main.chat.email",
        workspace="email",
        permissions={"ask": ["mcp.email.send"]},
    )

    assert profile.permissions.decisions == {"mcp.email.send": "allow"}
    assert route.permissions.decisions == {"mcp.email.send": "needs_human"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        RouteConfig(
            source="connection.matrix.main.chat.email",
            workspace="email",
            permissions={"allow": ["mcp.email.send"]},
            capabilities={"mcp.email.send": "deny"},
        )


def test_permissions_are_available_on_every_operator_layer():
    permission = {"allow": ["desktop.computer.use"]}

    assert ProfileConfig(permissions=permission).permissions.allow
    assert WorkspaceConfig(permissions=permission).permissions.allow
    assert WorkspaceThreadConfig(
        name="benefits",
        workspace="unemployment",
        profiles=["computer-use"],
        permissions=permission,
    ).permissions.allow
    assert WorkspaceScopeConfig(
        workspace="daily-review",
        profiles=["computer-use"],
        permissions=permission,
    ).permissions.allow
    assert RouteConfig(
        source="connection.matrix.main.chat.benefits",
        workspace="unemployment",
        permissions=permission,
    ).permissions.allow


def test_workspace_permissions_override_profile_policy_and_reach_semantic_children():
    settings = make_settings(
        profiles={"computer-use": ProfileConfig(permissions={"ask": ["desktop.computer.use"]})},
        workspaces={
            "finance": WorkspaceConfig(
                profiles=["computer-use"],
                permissions={"deny": ["desktop.computer.use"]},
                threads=[
                    WorkspaceThreadConfig(
                        name="benefits",
                        workspace="unemployment",
                        profiles=["computer-use"],
                        permissions={"allow": ["desktop.computer.use"]},
                    )
                ],
            )
        },
    )

    finance = settings.resolved_workspace_config("finance")
    unemployment = settings.resolved_workspace_config("unemployment")

    assert finance is not None
    assert finance.capabilities["desktop.computer.use"].decision == "deny"
    assert unemployment is not None
    assert unemployment.capabilities["desktop.computer.use"].decision == "needs_human"


def test_profile_and_workspace_permissions_merge_most_restrictively():
    settings = make_settings(
        profiles={
            "allow": ProfileConfig(permissions={"allow": ["mcp.email.send"]}),
            "ask": ProfileConfig(permissions={"ask": ["mcp.email.send"]}),
            "deny": ProfileConfig(permissions={"deny": ["mcp.email.send"]}),
        },
        workspaces={
            "email": WorkspaceConfig(
                profiles=["deny", "allow", "ask"],
                permissions={"allow": ["mcp.email.send"]},
            )
        },
    )

    resolved = settings.resolved_workspace_config("email")

    assert resolved is not None
    assert resolved.capabilities["mcp.email.send"].decision == "deny"


def test_runtime_allow_overrides_implicit_ask():
    settings = make_settings(
        profiles={"base": ProfileConfig(tools=["matrix_route_read"])},
        workspaces={"support": WorkspaceConfig(profiles=["base"])},
    )
    register_runtime_workspace_policy(
        "support-conversation-conv_test",
        RuntimeWorkspacePolicy(
            parent_workspace="support",
            capabilities={"chat.matrix.route.read": CapabilityRule(decision="allow")},
        ),
    )
    try:
        with patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings):
            resolved = load_resolved_config("support-conversation-conv_test")
    finally:
        clear_runtime_workspace_policies()

    assert resolved is not None
    assert resolved.capabilities["chat.matrix.route.read"].decision == "allow"
