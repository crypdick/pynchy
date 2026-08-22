"""Behavioral checks for reusable approval choices."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import NullIpcDeps, make_host_action_catalog, make_settings

from pynchy.config.api import ProfileConfig, WorkspaceConfig
from pynchy.host.container_manager.ipc.approval_grants import (
    apply_reusable_approval,
    resolve_mcp_proxy_approval_decision,
)
from pynchy.host.container_manager.ipc.approval_replay import (
    ApprovalDecisionContext,
    ApprovalReplayPolicy,
)
from pynchy.host.container_manager.security.gate import SecurityGate
from pynchy.host.orchestrator.workspace_config import update_workspace_capability_policy
from pynchy.workspace.api import CapabilityRule, WorkspaceSecurity


def _context(*, scope: str, gate: SecurityGate) -> ApprovalDecisionContext:
    catalog = make_host_action_catalog("my_tool", handler=MagicMock())
    action = catalog.action_for("my_tool")
    assert action is not None
    return ApprovalDecisionContext(
        request_id="request-1",
        source_group="calendar",
        tool_name="my_tool",
        chat_jid="discord:channel:1",
        request_data={},
        approved=True,
        approver="operator",
        approved_at="2026-08-19T12:00:00+00:00",
        handler_type="service",
        action=action,
        gate=gate,
        capability_id="test.my.tool",
        action_ids=("test.my.tool",),
        origin_conversation_id=None,
        action_payload=None,
        action_payload_sha256=None,
        requested_at="2026-08-19T11:59:00+00:00",
        expires_after_seconds=300,
        approval_scope=scope,
    )


@pytest.mark.asyncio
async def test_session_choice_grants_only_active_capability() -> None:
    replay_gate = SecurityGate(WorkspaceSecurity())
    active_gate = SecurityGate(WorkspaceSecurity())

    with patch(
        "pynchy.host.container_manager.ipc.approval_grants.get_gate_for_group",
        return_value=active_gate,
    ):
        assert await apply_reusable_approval(
            _context(scope="session", gate=replay_gate), NullIpcDeps()
        )

    assert replay_gate.has_session_capability_approval("test.my.tool")
    assert active_gate.has_session_capability_approval("test.my.tool")


@pytest.mark.asyncio
async def test_session_choice_supports_mcp_proxy_capability() -> None:
    replay_gate = SecurityGate(WorkspaceSecurity())
    context = replace(
        _context(scope="session", gate=replay_gate),
        handler_type="mcp_proxy",
        action=None,
        capability_id="mcp.linear.linear_get_issue",
    )

    with patch(
        "pynchy.host.container_manager.ipc.approval_grants.get_gate_for_group",
        return_value=replay_gate,
    ):
        assert await apply_reusable_approval(context, NullIpcDeps())

    assert replay_gate.has_session_capability_approval("mcp.linear.linear_get_issue")


@pytest.mark.asyncio
@pytest.mark.parametrize("validation_error", ["policy changed", None])
async def test_mcp_reusable_failure_denies_waiting_proxy(validation_error: str | None) -> None:
    deps = NullIpcDeps()
    deps.broadcast_host_message = AsyncMock()
    context = replace(
        _context(scope="session", gate=SecurityGate(WorkspaceSecurity())),
        handler_type="mcp_proxy",
        action=None,
        capability_id="mcp.linear.linear_get_issue",
    )
    reusable = AsyncMock(return_value=False)

    with (
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.approval_replay_validation_error",
            new=AsyncMock(return_value=validation_error),
        ),
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.apply_reusable_approval",
            new=reusable,
        ),
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.security_approval.resolve_mcp_proxy_approval",
            return_value=True,
        ) as resolve,
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.record_security_event",
            new=AsyncMock(),
        ),
    ):
        await resolve_mcp_proxy_approval_decision(
            context,
            deps,
            ApprovalReplayPolicy(
                configured_security=lambda _group: WorkspaceSecurity(),
                workspace_tools=lambda _group: (),
            ),
        )

    resolve.assert_called_once_with("request-1", approved=False)
    if validation_error is None:
        reusable.assert_awaited_once()
    else:
        reusable.assert_not_awaited()
        deps.broadcast_host_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcp_reusable_without_replay_dependencies_denies_waiting_proxy() -> None:
    context = replace(
        _context(scope="session", gate=SecurityGate(WorkspaceSecurity())),
        handler_type="mcp_proxy",
        action=None,
        capability_id="mcp.linear.linear_get_issue",
    )

    with (
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.security_approval.resolve_mcp_proxy_approval",
            return_value=True,
        ) as resolve,
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.record_security_event",
            new=AsyncMock(),
        ),
    ):
        await resolve_mcp_proxy_approval_decision(
            context,
            None,
            ApprovalReplayPolicy(
                configured_security=lambda _group: WorkspaceSecurity(),
                workspace_tools=lambda _group: (),
            ),
        )

    resolve.assert_called_once_with("request-1", approved=False)


@pytest.mark.asyncio
async def test_mcp_reusable_choice_without_capability_has_no_ipc_side_effect() -> None:
    deps = NullIpcDeps()
    deps.fail_action_intent = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    context = replace(
        _context(scope="session", gate=SecurityGate(WorkspaceSecurity())),
        handler_type="mcp_proxy",
        action=None,
        capability_id=None,
    )

    with patch(
        "pynchy.host.container_manager.ipc.approval_grants.record_security_event",
        new=AsyncMock(),
    ):
        assert not await apply_reusable_approval(context, deps)

    deps.fail_action_intent.assert_not_awaited()
    deps.broadcast_host_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_forever_choice_persists_current_capability() -> None:
    deps = NullIpcDeps()
    deps.persist_capability_approval = MagicMock()

    assert await apply_reusable_approval(
        _context(scope="forever", gate=SecurityGate(WorkspaceSecurity())), deps
    )

    deps.persist_capability_approval.assert_called_once_with("calendar", "test.my.tool")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_action", [True, False])
async def test_reusable_choice_fails_closed_without_semantic_capability(
    missing_action: bool,
) -> None:
    deps = NullIpcDeps()
    deps.fail_action_intent = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    context = _context(scope="session", gate=SecurityGate(WorkspaceSecurity()))
    context = replace(
        context,
        action=None if missing_action else context.action,
        handler_type="ipc" if not missing_action else context.handler_type,
    )

    with (
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.ipc_response_path",
            return_value=MagicMock(),
        ),
        patch("pynchy.host.container_manager.ipc.approval_grants.write_ipc_response"),
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.record_security_event",
            new=AsyncMock(),
        ),
    ):
        assert not await apply_reusable_approval(context, deps)

    deps.fail_action_intent.assert_awaited_once()
    deps.broadcast_host_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_forever_choice_reports_persistence_failure() -> None:
    deps = NullIpcDeps()
    deps.persist_capability_approval = MagicMock(side_effect=ValueError("blocked"))
    deps.fail_action_intent = AsyncMock()
    deps.broadcast_host_message = AsyncMock()

    with (
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.ipc_response_path",
            return_value=MagicMock(),
        ),
        patch("pynchy.host.container_manager.ipc.approval_grants.write_ipc_response"),
        patch(
            "pynchy.host.container_manager.ipc.approval_grants.record_security_event",
            new=AsyncMock(),
        ),
    ):
        assert not await apply_reusable_approval(
            _context(scope="forever", gate=SecurityGate(WorkspaceSecurity())), deps
        )

    deps.broadcast_host_message.assert_awaited_once_with(
        "discord:channel:1", "Could not approve forever: blocked"
    )


def test_forever_choice_updates_owning_workspace_document(tmp_path) -> None:
    path = tmp_path / "data/personalization/workspaces/calendar.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'schema_version = 1\n\n[workspace]\nprofiles = ["base"]\n'
        'permissions = { ask = ["calendar.event.list"] }\n',
        encoding="utf-8",
    )
    settings = make_settings(
        project_root=tmp_path,
        profiles={"base": ProfileConfig()},
        workspaces={"calendar": WorkspaceConfig(profiles=["base"])},
    )
    resolved = MagicMock(capabilities={"calendar.event.list": CapabilityRule("allow")})
    publish = MagicMock(return_value="pushed")

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
    ):
        update_workspace_capability_policy("calendar", "calendar.event.list", publish=publish)

    publish.assert_called_once_with(tmp_path)
    permissions = tomllib.loads(path.read_text(encoding="utf-8"))["workspace"]["permissions"]
    assert permissions == {
        "allow": ["calendar.event.list"],
        "ask": [],
        "deny": [],
    }


@pytest.mark.parametrize(
    ("publication", "error", "message"),
    [
        ("failed", ValueError, "publication failed"),
        ("updated", ValueError, "publication failed"),
        (OSError("offline"), OSError, "offline"),
    ],
)
def test_forever_choice_rolls_back_when_publication_fails(
    tmp_path, publication: str | OSError, error: type[Exception], message: str
) -> None:
    path = tmp_path / "data/personalization/workspaces/calendar.toml"
    path.parent.mkdir(parents=True)
    original = '[workspace]\nprofiles = ["base"]\n'
    path.write_text(original, encoding="utf-8")
    settings = make_settings(
        project_root=tmp_path,
        profiles={"base": ProfileConfig()},
        workspaces={"calendar": WorkspaceConfig(profiles=["base"])},
    )
    resolved = MagicMock(capabilities={"calendar.event.list": CapabilityRule("allow")})

    def publish(_root) -> str:
        if isinstance(publication, OSError):
            raise publication
        return publication

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
        pytest.raises(error, match=message),
    ):
        update_workspace_capability_policy("calendar", "calendar.event.list", publish=publish)

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("document", "message"),
    [(None, "no personalized declaration"), ("[other]\nvalue = 1\n", "declaration is invalid")],
)
def test_forever_choice_rejects_missing_workspace_document(
    tmp_path, document: str | None, message: str
) -> None:
    path = tmp_path / "data/personalization/workspaces/calendar.toml"
    if document is not None:
        path.parent.mkdir(parents=True)
        path.write_text(document, encoding="utf-8")
    settings = make_settings(
        project_root=tmp_path,
        profiles={"base": ProfileConfig()},
        workspaces={"calendar": WorkspaceConfig(profiles=["base"])},
    )

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        pytest.raises(ValueError, match=message),
    ):
        update_workspace_capability_policy(
            "calendar", "calendar.event.list", publish=lambda _root: "pushed"
        )


def test_forever_choice_adds_permissions_when_workspace_has_none(tmp_path) -> None:
    path = tmp_path / "data/personalization/workspaces/calendar.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[workspace]\nprofiles = ["base"]\n', encoding="utf-8")
    settings = make_settings(
        project_root=tmp_path,
        profiles={"base": ProfileConfig()},
        workspaces={"calendar": WorkspaceConfig(profiles=["base"])},
    )
    resolved = MagicMock(capabilities={"calendar.event.list": CapabilityRule("allow")})

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
    ):
        update_workspace_capability_policy(
            "calendar", "calendar.event.list", publish=lambda _root: "pushed"
        )

    permissions = tomllib.loads(path.read_text(encoding="utf-8"))["workspace"]["permissions"]
    assert permissions["allow"] == ["calendar.event.list"]


def test_forever_choice_rolls_back_when_inherited_policy_is_stricter(tmp_path) -> None:
    path = tmp_path / "data/personalization/workspaces/calendar.toml"
    path.parent.mkdir(parents=True)
    original = '[workspace]\nprofiles = ["base"]\n'
    path.write_text(original, encoding="utf-8")
    settings = make_settings(
        project_root=tmp_path,
        profiles={"base": ProfileConfig()},
        workspaces={"calendar": WorkspaceConfig(profiles=["base"])},
    )
    resolved = MagicMock(capabilities={"calendar.*": CapabilityRule("needs_human")})

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
        pytest.raises(ValueError, match="stricter inherited permission"),
    ):
        update_workspace_capability_policy(
            "calendar", "calendar.event.list", publish=lambda _root: "pushed"
        )

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("collection", ["scopes", "threads"])
def test_forever_choice_updates_semantic_workspace_policy(tmp_path, collection: str) -> None:
    child = f"calendar-{collection}"
    other = f"calendar-other-{collection}"
    path = tmp_path / "data/personalization/workspaces/calendar.toml"
    path.parent.mkdir(parents=True)
    identifying_field = 'name = "Approvals"\n' if collection == "threads" else ""
    other_identifying_field = 'name = "Other"\n' if collection == "threads" else ""
    path.write_text(
        '[workspace]\nprofiles = ["base"]\n\n'
        f'[[workspace.{collection}]]\n{other_identifying_field}workspace = "{other}"\n'
        'profiles = ["base"]\n\n'
        f'[[workspace.{collection}]]\n{identifying_field}workspace = "{child}"\n'
        'profiles = ["base"]\n',
        encoding="utf-8",
    )
    child_config = {
        "workspace": child,
        "profiles": ["base"],
        **({"name": "Approvals"} if collection == "threads" else {}),
    }
    other_config = {
        "workspace": other,
        "profiles": ["base"],
        **({"name": "Other"} if collection == "threads" else {}),
    }
    settings = make_settings(
        project_root=tmp_path,
        profiles={"base": ProfileConfig()},
        workspaces={
            "calendar": WorkspaceConfig(
                profiles=["base"],
                **{collection: [other_config, child_config]},
            )
        },
    )
    resolved = MagicMock(capabilities={"calendar.event.list": CapabilityRule("allow")})

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        patch(
            "pynchy.host.orchestrator.workspace_config.load_resolved_config",
            return_value=resolved,
        ),
    ):
        update_workspace_capability_policy(
            child, "calendar.event.list", publish=lambda _root: "pushed"
        )

    children = tomllib.loads(path.read_text(encoding="utf-8"))["workspace"][collection]
    child_doc = next(candidate for candidate in children if candidate["workspace"] == child)
    assert child_doc["permissions"]["allow"] == ["calendar.event.list"]


def test_forever_choice_rejects_semantic_workspace_without_document_owner(tmp_path) -> None:
    path = tmp_path / "data/personalization/workspaces/calendar.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[workspace]\nprofiles = ["base"]\n', encoding="utf-8")
    settings = make_settings(
        project_root=tmp_path,
        profiles={"base": ProfileConfig()},
        workspaces={
            "calendar": WorkspaceConfig(
                profiles=["base"],
                scopes=[{"workspace": "calendar-child", "profiles": ["base"]}],
            )
        },
    )

    with (
        patch("pynchy.host.orchestrator.workspace_config.get_settings", return_value=settings),
        pytest.raises(ValueError, match="no persistent policy owner"),
    ):
        update_workspace_capability_policy(
            "calendar-child", "calendar.event.list", publish=lambda _root: "pushed"
        )
