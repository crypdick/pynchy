"""Tests for operator-authored permission policy configuration."""

import pytest
from conftest import make_settings
from pydantic import ValidationError

from pynchy.config.api import PermissionConfig, ProfileConfig, WorkspaceConfig
from pynchy.config.models import RouteConfig
from pynchy.config.workspace_layout import WorkspaceScopeConfig, WorkspaceThreadConfig


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


def test_profile_rejects_new_and_legacy_policy_together():
    with pytest.raises(ValidationError, match=r"permissions.*capabilities"):
        ProfileConfig(
            permissions={"allow": ["mcp.email.send"]},
            capabilities={"mcp.email.send": {"decision": "allow"}},
        )


def test_new_permission_syntax_resolves_for_profiles_and_routes():
    profile = ProfileConfig(permissions={"allow": ["mcp.email.send"]})
    route = RouteConfig(
        source="connection.matrix.main.chat.email",
        workspace="email",
        permissions={"ask": ["mcp.email.send"]},
    )

    assert profile.permission_decisions == {"mcp.email.send": "allow"}
    assert route.permission_decisions == {"mcp.email.send": "needs_human"}

    with pytest.raises(ValidationError, match=r"permissions.*capabilities"):
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
        profiles={
            "computer-use": ProfileConfig(
                capabilities={"desktop.computer.use": {"decision": "needs_human"}}
            )
        },
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
    assert unemployment.capabilities["desktop.computer.use"].decision == "allow"
