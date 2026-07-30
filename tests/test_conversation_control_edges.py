"""Public behavior tests for conversation control reconciliation."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.conversation.api import (
    ControlSurface,
    Conversation,
    ConversationControlBinding,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    routed_conversation_folder,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlClosedError,
    ConversationControlRequest,
    ConversationControlWorkspaceChangedError,
    ConversationWorkspaceContext,
    EnsuredConversationControl,
    ensure_conversation_control,
    ensure_conversation_workspace,
    sync_conversation_control_state,
    sync_existing_open_conversation_control,
)
from pynchy.host.orchestrator.threads import EnsuredThread
from pynchy.host.orchestrator.workspace_placement import WorkspacePlacement
from pynchy.identifiers import ChatJid, GroupFolder, SessionId
from pynchy.workspace.api import WorkspaceProfile


def _conversation(
    *,
    closed: bool = False,
    workspace: str = "owner",
    session_id: str | None = None,
) -> Conversation:
    return Conversation(
        id=ConversationId("conversation-1"),
        workspace=GroupFolder(workspace),
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        session_id=SessionId(session_id) if session_id is not None else None,
        created_at="2026-07-30T00:00:00+00:00",
        updated_at="2026-07-30T00:00:00+00:00",
        control_closed=closed,
    )


def _request(*, title: str = "Readable title", owner: str | None = "owner"):
    return ConversationControlRequest(
        conversation_id=ConversationId("conversation-1"),
        parent_workspace=GroupFolder("control"),
        parent_jid=ChatJid("discord:channel:control"),
        title=title,
        owner_workspace=GroupFolder(owner) if owner is not None else None,
    )


def _binding(*, thread: str = "thread-1", closed: bool = False) -> ConversationControlBinding:
    return ConversationControlBinding(
        conversation_id=ConversationId("conversation-1"),
        surface=ControlSurface.DISCORD,
        parent_workspace=GroupFolder("control"),
        parent_jid=ChatJid("discord:channel:control"),
        thread_jid=ChatJid(f"discord:channel:{thread}"),
        title="Readable title",
        updated_at=datetime.now(UTC).isoformat(),
        closed=closed,
    )


def _profile(jid: str, folder: str, *, name: str | None = None) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=jid,
        name=name or folder.title(),
        folder=folder,
        trigger="@Pynchy",
        added_at="2026-07-30T00:00:00+00:00",
    )


def _workspace_context(
    workspaces: dict[str, WorkspaceProfile],
    *,
    rebind_workspace=None,
) -> ConversationWorkspaceContext:
    return ConversationWorkspaceContext(
        channels=list,
        workspaces=lambda: workspaces,
        register_workspace=AsyncMock(),
        unregister_workspace=AsyncMock(),
        bind_session=AsyncMock(),
        rebind_workspace=rebind_workspace,
    )


def test_control_request_rejects_blank_title():
    with pytest.raises(ValueError, match="title must not be empty"):
        _request(title="  ")


@pytest.mark.parametrize(
    ("control_request", "parent", "message"),
    [
        (_request(), None, "Unknown conversation"),
        (
            ConversationControlRequest(
                conversation_id=ConversationId("conversation-1"),
                parent_workspace=GroupFolder("control"),
                parent_jid=ChatJid("slack:channel:control"),
                title="Readable title",
            ),
            None,
            "must belong to Discord",
        ),
        (_request(), None, "registered workspace root"),
    ],
)
async def test_ensure_control_rejects_invalid_conversation_or_parent(
    control_request: ConversationControlRequest,
    parent: WorkspaceProfile | None,
    message: str,
):
    conversation = None if message == "Unknown conversation" else _conversation()
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=conversation),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_workspace_profile",
            new=AsyncMock(return_value=parent),
        ),
        pytest.raises(ValueError, match=message),
    ):
        await ensure_conversation_control([], control_request)


async def test_closed_control_requires_binding_but_returns_existing_closed_binding():
    conversation = _conversation(closed=True)
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=conversation),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_workspace_profile",
            new=AsyncMock(return_value=_profile("discord:channel:control", "control")),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(side_effect=[None, _binding()]),
        ),
    ):
        with pytest.raises(ConversationControlClosedError):
            await ensure_conversation_control([], _request())

        ensured = await ensure_conversation_control([], _request())

    assert ensured.created is False
    assert ensured.binding.closed is True


async def test_ensure_control_rejects_thread_without_a_jid():
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_workspace_profile",
            new=AsyncMock(return_value=_profile("discord:channel:control", "control")),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_thread",
            new=AsyncMock(return_value=EnsuredThread(jid=None, created=True)),
        ),
        pytest.raises(RuntimeError, match="no chat JID"),
    ):
        await ensure_conversation_control([], _request())


async def test_ensure_control_suffixes_a_thread_owned_by_another_conversation():
    other = replace(_binding(thread="owned-by-other"), conversation_id=ConversationId("other"))
    ensured_thread = AsyncMock(
        side_effect=[
            EnsuredThread(jid="discord:channel:thread-1", created=True),
            EnsuredThread(jid="discord:channel:thread-2", created=True),
        ]
    )
    owner_lookup = AsyncMock(side_effect=[other, None])
    saved = _binding(thread="thread-2")

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_workspace_profile",
            new=AsyncMock(return_value=_profile("discord:channel:control", "control")),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_thread",
            new=ensured_thread,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_by_thread",
            new=owner_lookup,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.set_conversation_control_binding",
            new=AsyncMock(return_value=saved),
        ),
    ):
        result = await ensure_conversation_control([], _request(title="A"))

    assert result.binding == saved
    assert [call.args[2] for call in ensured_thread.await_args_list] == ["A", "A (2)"]


async def test_ensure_control_retries_after_concurrent_binding_claim():
    other = replace(_binding(thread="owned-by-other"), conversation_id=ConversationId("other"))
    saved = _binding(thread="thread-2")
    ensure_thread = AsyncMock(
        side_effect=[
            EnsuredThread(jid="discord:channel:thread-1", created=True),
            EnsuredThread(jid="discord:channel:thread-2", created=True),
        ]
    )
    lookup = AsyncMock(side_effect=[None, other, None])
    persist = AsyncMock(side_effect=[sqlite3.IntegrityError("claimed"), saved])

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_workspace_profile",
            new=AsyncMock(return_value=_profile("discord:channel:control", "control")),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_thread",
            new=ensure_thread,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_by_thread",
            new=lookup,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.set_conversation_control_binding",
            new=persist,
        ),
    ):
        result = await ensure_conversation_control([], _request(title="A"))

    assert result.binding == saved
    assert persist.await_count == 2


async def test_ensure_control_reraises_unconfirmed_binding_collision():
    persist = AsyncMock(side_effect=sqlite3.IntegrityError("claimed"))
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_workspace_profile",
            new=AsyncMock(return_value=_profile("discord:channel:control", "control")),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_thread",
            new=AsyncMock(return_value=EnsuredThread(jid="discord:channel:thread-1", created=True)),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_by_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.set_conversation_control_binding",
            new=persist,
        ),
        pytest.raises(sqlite3.IntegrityError),
    ):
        await ensure_conversation_control([], _request())


async def test_ensure_control_closes_a_binding_persisted_as_closed():
    saved = _binding(closed=True)
    close_thread = AsyncMock()
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_workspace_profile",
            new=AsyncMock(return_value=_profile("discord:channel:control", "control")),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_thread",
            new=AsyncMock(
                return_value=EnsuredThread(jid="discord:channel:thread-1", created=False)
            ),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_by_thread",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.set_conversation_control_binding",
            new=AsyncMock(return_value=saved),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.set_thread_closed",
            new=close_thread,
        ),
    ):
        result = await ensure_conversation_control([], _request())

    assert result.created is False
    close_thread.assert_awaited_once_with([], saved.thread_jid, closed=True)


async def test_sync_control_state_applies_terminal_intent_to_existing_binding():
    close_thread = AsyncMock()
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation(closed=True)),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(return_value=_binding()),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.set_thread_closed",
            new=close_thread,
        ),
    ):
        await sync_conversation_control_state([], ConversationId("conversation-1"))

    close_thread.assert_awaited_once_with([], ChatJid("discord:channel:thread-1"), closed=True)


async def test_sync_control_state_rejects_unknown_conversation_and_ignores_missing_binding():
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(ValueError, match="Unknown conversation"),
    ):
        await sync_conversation_control_state([], ConversationId("conversation-1"))

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_control_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.set_thread_closed",
            new=AsyncMock(),
        ) as close_thread,
    ):
        await sync_conversation_control_state([], ConversationId("conversation-1"))

    close_thread.assert_not_awaited()


async def test_sync_existing_open_control_rechecks_state_inside_runtime_lock():
    sync_state = AsyncMock()
    conversation = _conversation()
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_for_subject",
            new=AsyncMock(return_value=conversation),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=conversation),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.sync_conversation_control_state",
            new=sync_state,
        ),
    ):
        await sync_existing_open_conversation_control([], conversation.subject)

    sync_state.assert_awaited_once_with([], conversation.id)


async def test_sync_existing_open_control_ignores_missing_or_newly_closed_conversation():
    subject = _conversation().subject
    with patch(
        "pynchy.host.orchestrator.conversation_control.get_conversation_for_subject",
        new=AsyncMock(return_value=None),
    ):
        await sync_existing_open_conversation_control([], subject)

    conversation = _conversation(closed=True)
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation_for_subject",
            new=AsyncMock(return_value=conversation),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=conversation),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.sync_conversation_control_state",
            new=AsyncMock(),
        ) as sync_state,
    ):
        await sync_existing_open_conversation_control([], subject)

    sync_state.assert_not_awaited()


async def test_workspace_placement_rejects_missing_parent_or_owner():
    request = _request()
    context = _workspace_context({})
    with pytest.raises(ValueError, match="parent workspace"):
        await ensure_conversation_workspace(context, request)

    parent = _profile("discord:channel:control", "control")
    context = _workspace_context({parent.jid: parent})
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=None,
        ),
        pytest.raises(ValueError, match="policy owner"),
    ):
        await ensure_conversation_workspace(context, request)


async def test_workspace_placement_rejects_closed_or_disappeared_conversation():
    parent = _profile("discord:channel:control", "control")
    owner = _profile("discord:channel:owner", "owner")
    context = _workspace_context({parent.jid: parent, owner.jid: owner})
    placement = WorkspacePlacement(owner=owner, control_parent=parent)
    request = _request()
    closed_control = EnsuredConversationControl(_binding(closed=True), created=False)

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=closed_control),
        ),
        pytest.raises(ConversationControlClosedError),
    ):
        await ensure_conversation_workspace(context, request)

    open_control = EnsuredConversationControl(_binding(), created=False)
    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=open_control),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=None),
        ),
        pytest.raises(RuntimeError, match="disappeared"),
    ):
        await ensure_conversation_workspace(context, request)


async def test_workspace_placement_rejects_late_close_or_owner_change():
    parent = _profile("discord:channel:control", "control")
    owner = _profile("discord:channel:owner", "owner")
    context = _workspace_context({parent.jid: parent, owner.jid: owner})
    placement = WorkspacePlacement(owner=owner, control_parent=parent)
    request = _request()
    open_control = EnsuredConversationControl(_binding(), created=False)

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=open_control),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation(closed=True)),
        ),
        pytest.raises(ConversationControlClosedError),
    ):
        await ensure_conversation_workspace(context, request)

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=open_control),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation(workspace="other")),
        ),
        pytest.raises(ConversationControlWorkspaceChangedError),
    ):
        await ensure_conversation_workspace(context, request)


async def test_workspace_placement_rebinds_prior_thread_and_binds_session():
    parent = _profile("discord:channel:control", "control")
    owner = _profile("discord:channel:owner", "owner")
    prior = _profile(
        "discord:channel:old",
        routed_conversation_folder("owner", ConversationId("conversation-1")),
    )
    rebound = AsyncMock()
    context = _workspace_context(
        {parent.jid: parent, owner.jid: owner, prior.jid: prior},
        rebind_workspace=rebound,
    )
    placement = WorkspacePlacement(owner=owner, control_parent=parent)

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=EnsuredConversationControl(_binding(), created=True)),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation(session_id="session-1")),
        ),
    ):
        result = await ensure_conversation_workspace(context, _request())

    rebound.assert_awaited_once_with(result.profile)
    context.bind_session.assert_awaited_once_with(result.profile.folder, SessionId("session-1"))


async def test_workspace_placement_unregisters_prior_thread_without_rebind_support():
    parent = _profile("discord:channel:control", "control")
    owner = _profile("discord:channel:owner", "owner")
    prior = _profile(
        "discord:channel:old",
        routed_conversation_folder("owner", ConversationId("conversation-1")),
    )
    context = _workspace_context({parent.jid: parent, owner.jid: owner, prior.jid: prior})
    placement = WorkspacePlacement(owner=owner, control_parent=parent)

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=EnsuredConversationControl(_binding(), created=False)),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
    ):
        await ensure_conversation_workspace(context, _request())

    context.unregister_workspace.assert_awaited_once_with(prior.jid)
    context.register_workspace.assert_awaited_once()


async def test_workspace_placement_registers_missing_current_profile():
    parent = _profile("discord:channel:control", "control")
    owner = _profile("discord:channel:owner", "owner")
    context = _workspace_context({parent.jid: parent, owner.jid: owner})
    placement = WorkspacePlacement(owner=owner, control_parent=parent)

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=EnsuredConversationControl(_binding(), created=False)),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
    ):
        result = await ensure_conversation_workspace(context, _request())

    context.register_workspace.assert_awaited_once_with(result.profile)


async def test_workspace_placement_skips_registration_when_current_profile_matches():
    parent = _profile("discord:channel:control", "control")
    owner = _profile("discord:channel:owner", "owner")
    binding = _binding()
    routed_folder = routed_conversation_folder("owner", ConversationId("conversation-1"))
    current = _profile(
        str(binding.thread_jid),
        routed_folder,
        name="Owner/Readable title",
    )
    context = _workspace_context({parent.jid: parent, owner.jid: owner, current.jid: current})
    placement = WorkspacePlacement(owner=owner, control_parent=parent)

    with (
        patch(
            "pynchy.host.orchestrator.conversation_control.resolve_workspace_placement",
            return_value=placement,
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.ensure_conversation_control",
            new=AsyncMock(return_value=EnsuredConversationControl(binding, created=False)),
        ),
        patch(
            "pynchy.host.orchestrator.conversation_control.get_conversation",
            new=AsyncMock(return_value=_conversation()),
        ),
    ):
        await ensure_conversation_workspace(context, _request())

    context.register_workspace.assert_not_awaited()
