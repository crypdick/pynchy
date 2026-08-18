"""Tests for startup restoration of routed workspace policy."""

from unittest.mock import AsyncMock, MagicMock, patch

from conftest import init_test_database, make_settings

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config.api import ProfileConfig, WorkspaceConfig
from pynchy.conversation.models import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.orchestrator.routed_workspace_policy import (
    restore_routed_workspace_policy_owners,
)
from pynchy.host.orchestrator.workspace_config import load_resolved_config
from pynchy.identifiers import GroupFolder
from pynchy.state import resolve_conversation
from pynchy.workspace.api import WorkspaceProfile


async def test_startup_restores_policy_for_deliveryless_open_conversation(tmp_path) -> None:
    await init_test_database()
    settings = make_settings(
        profiles={
            "support": ProfileConfig(
                repo="owner/project",
                execution_mode="host",
                cwd=str(tmp_path / "project"),
                is_admin=True,
            )
        },
        workspaces={"support": WorkspaceConfig(profiles=["support"])},
    )
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:team:issue"),
            key=ConversationSubjectKey("SYN-148"),
        ),
        GroupFolder("support"),
    )
    routed = WorkspaceProfile(
        jid="discord:channel:routed",
        name="Support/SYN-148",
        folder=routed_conversation_folder("support", conversation.id),
        trigger="@pynchy",
        is_admin=True,
    )

    try:
        with patch(
            "pynchy.host.orchestrator.workspace_config.get_settings",
            return_value=settings,
        ):
            assert load_resolved_config(routed.folder) is None
            await restore_routed_workspace_policy_owners([routed])
            resolved = load_resolved_config(routed.folder)
    finally:
        workspace_config.clear_runtime_workspace_policies()

    assert resolved is not None
    assert resolved.repo == ["owner/project"]
    assert resolved.execution_mode == "host"


async def test_startup_does_not_restore_policy_for_closed_conversation(monkeypatch) -> None:
    routed = WorkspaceProfile(
        jid="discord:channel:routed",
        name="Closed",
        folder=routed_conversation_folder("support", "conv_closed"),
        trigger="@pynchy",
        is_admin=True,
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.routed_workspace_policy.get_conversation",
        AsyncMock(return_value=MagicMock(control_closed=True)),
    )
    ensure_owner = MagicMock()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.routed_workspace_policy.ensure_runtime_workspace_policy_owner",
        ensure_owner,
    )

    await restore_routed_workspace_policy_owners([routed])

    ensure_owner.assert_not_called()


async def test_startup_does_not_restore_policy_for_missing_conversation(monkeypatch) -> None:
    routed = WorkspaceProfile(
        jid="discord:channel:routed",
        name="Missing",
        folder=routed_conversation_folder("support", "conv_missing"),
        trigger="@pynchy",
        is_admin=True,
    )
    monkeypatch.setattr(
        "pynchy.host.orchestrator.routed_workspace_policy.get_conversation",
        AsyncMock(return_value=None),
    )
    ensure_owner = MagicMock()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.routed_workspace_policy.ensure_runtime_workspace_policy_owner",
        ensure_owner,
    )

    await restore_routed_workspace_policy_owners([routed])

    ensure_owner.assert_not_called()


async def test_startup_ignores_a_workspace_without_a_routed_conversation(monkeypatch) -> None:
    workspace = WorkspaceProfile(
        jid="discord:channel:regular",
        name="Regular",
        folder="support",
        trigger="@pynchy",
        is_admin=True,
    )
    get_conversation = AsyncMock()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.routed_workspace_policy.get_conversation",
        get_conversation,
    )
    ensure_owner = MagicMock()
    monkeypatch.setattr(
        "pynchy.host.orchestrator.routed_workspace_policy.ensure_runtime_workspace_policy_owner",
        ensure_owner,
    )

    await restore_routed_workspace_policy_owners([workspace])

    get_conversation.assert_not_awaited()
    ensure_owner.assert_not_called()
