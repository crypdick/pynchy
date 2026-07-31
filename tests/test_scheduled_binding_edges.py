"""Additional public behavior tests for scheduled runtime binding."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from conftest import configure_workspace_placement_for, make_settings

from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlClosedError,
    ConversationControlWorkspaceChangedError,
    EnsuredConversationControl,
    EnsuredConversationWorkspace,
)
from pynchy.host.orchestrator.scheduled_binding import (
    ScheduledTaskOwnershipError,
    ScheduledTaskTerminalError,
    ensure_scheduled_task_binding,
    ensure_scheduled_task_conversation_open,
    reconcile_scheduled_task_bindings,
)
from pynchy.identifiers import ChatJid, GroupFolder
from pynchy.state import (
    create_task,
    get_task_by_id,
    init_test_database,
    resolve_conversation,
)
from tests.test_scheduled_binding import _BindingDeps, _profile, _task


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


async def test_existing_binding_with_wrong_folder_recreates_named_thread(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    bound = _profile(
        jid="discord:channel:scheduled-task",
        folder="owner__thread_discord-channel-scheduled-task",
    )
    task = replace(
        _task(),
        derived_thread_name="Owner | durable task",
        bound_chat_jid=bound.jid,
        bound_group_folder="different-folder",
    )
    await create_task(task)
    deps = _BindingDeps({owner.jid: owner, bound.jid: bound})

    ensured = await ensure_scheduled_task_binding(task, deps)

    assert deps.ensured == [(owner.jid, task.derived_thread_name)]
    assert ensured.bound_chat_jid == deps.ensured_jid


async def test_startup_reconciles_active_and_paused_automation_bindings() -> None:
    active = _task()
    paused = replace(active, id="task-paused", status="paused")
    cancelled = replace(active, id="task-cancelled", status="cancelled")
    routed = replace(active, id="task-routed", conversation_id="conversation-1")
    ensure = AsyncMock(side_effect=[active, OSError("obsolete owner")])

    with patch(
        "pynchy.host.orchestrator.scheduled_binding.ensure_scheduled_task_binding",
        ensure,
    ):
        reconciled = await reconcile_scheduled_task_bindings(
            [active, paused, cancelled, routed],
            _BindingDeps({}),
        )

    assert reconciled == 1
    assert [call.args[0].id for call in ensure.await_args_list] == [
        active.id,
        paused.id,
    ]


async def test_existing_binding_from_another_owner_recreates_named_thread(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    bound = _profile(
        jid="discord:channel:scheduled-task",
        folder="other__thread_discord-channel-scheduled-task",
    )
    task = replace(
        _task(),
        derived_thread_name="Owner | durable task",
        bound_chat_jid=bound.jid,
        bound_group_folder=bound.folder,
    )
    await create_task(task)
    deps = _BindingDeps({owner.jid: owner, bound.jid: bound})

    ensured = await ensure_scheduled_task_binding(task, deps)

    assert deps.ensured == [(owner.jid, task.derived_thread_name)]
    assert ensured.bound_group_folder != bound.folder


async def test_linear_routed_task_rejects_non_linear_issue_conversation(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("github:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(owner.folder),
    )
    task = replace(
        _task(),
        input_source="external:linear:human_approved",
        conversation_id=str(conversation.id),
        derived_thread_name="Owner | durable task",
    )
    await create_task(task)

    with pytest.raises(ScheduledTaskOwnershipError, match="non-Linear issue"):
        await ensure_scheduled_task_binding(task, _BindingDeps({owner.jid: owner}))


async def test_routed_task_rejects_a_conversation_without_an_owner(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("provider:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("missing-owner"),
    )
    task = replace(
        _task(),
        conversation_id=str(conversation.id),
        derived_thread_name="Owner | durable task",
    )
    await create_task(task)

    with pytest.raises(ScheduledTaskOwnershipError, match="owner workspace is unavailable"):
        await ensure_scheduled_task_binding(task, _BindingDeps({owner.jid: owner}))


async def test_routed_binding_cancels_when_control_is_closed(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("provider:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(owner.folder),
    )
    task = replace(
        _task(),
        conversation_id=str(conversation.id),
        derived_thread_name="Owner | durable task",
    )
    await create_task(task)
    deps = _BindingDeps({owner.jid: owner})
    with (
        patch(
            "pynchy.host.orchestrator.scheduled_binding.ensure_conversation_workspace",
            AsyncMock(side_effect=ConversationControlClosedError("closed")),
        ),
        pytest.raises(ScheduledTaskTerminalError, match="terminal conversation"),
    ):
        await ensure_scheduled_task_binding(task, deps)

    persisted = await get_task_by_id(task.id)
    assert persisted is not None
    assert persisted.status == "cancelled"


async def test_routed_binding_retries_when_workspace_changes(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("provider:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(owner.folder),
    )
    task = replace(
        _task(),
        conversation_id=str(conversation.id),
        derived_thread_name="Owner | durable task",
    )
    await create_task(task)
    profile = _profile(
        jid="discord:channel:scheduled-task",
        folder="owner__thread_conversation-1",
    )
    binding = ConversationControlBinding(
        conversation_id=conversation.id,
        surface=ControlSurface.DISCORD,
        parent_workspace=GroupFolder(owner.folder),
        parent_jid=ChatJid(owner.jid),
        thread_jid=ChatJid(profile.jid),
        title=task.derived_thread_name,
        updated_at="2026-07-30T00:00:00+00:00",
    )
    ensured = EnsuredConversationWorkspace(
        profile=profile,
        control=EnsuredConversationControl(binding=binding, created=False),
    )
    ensure = AsyncMock(side_effect=[ConversationControlWorkspaceChangedError("changed"), ensured])
    with patch(
        "pynchy.host.orchestrator.scheduled_binding.ensure_conversation_workspace",
        ensure,
    ):
        bound = await ensure_scheduled_task_binding(task, _BindingDeps({owner.jid: owner}))

    assert bound.bound_chat_jid == profile.jid
    assert ensure.await_count == 2


async def test_routed_binding_rejects_two_consecutive_workspace_changes(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("provider:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(owner.folder),
    )
    task = replace(
        _task(),
        conversation_id=str(conversation.id),
        derived_thread_name="Owner | durable task",
    )
    await create_task(task)
    ensure = AsyncMock(side_effect=ConversationControlWorkspaceChangedError("changed twice"))
    with (
        patch(
            "pynchy.host.orchestrator.scheduled_binding.ensure_conversation_workspace",
            ensure,
        ),
        pytest.raises(ScheduledTaskOwnershipError, match="workspace changed"),
    ):
        await ensure_scheduled_task_binding(task, _BindingDeps({owner.jid: owner}))

    assert ensure.await_count == 2


async def test_routed_binding_cancels_when_ensured_control_is_closed(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("provider:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(owner.folder),
    )
    task = replace(
        _task(),
        conversation_id=str(conversation.id),
        derived_thread_name="Owner | durable task",
    )
    await create_task(task)
    binding = replace(
        ConversationControlBinding(
            conversation_id=conversation.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder(owner.folder),
            parent_jid=ChatJid(owner.jid),
            thread_jid=ChatJid("discord:channel:scheduled-task"),
            title=task.derived_thread_name,
            updated_at="2026-07-30T00:00:00+00:00",
        ),
        closed=True,
    )
    ensured = EnsuredConversationWorkspace(
        profile=_profile(jid=str(binding.thread_jid), folder="owner__thread_scheduled"),
        control=EnsuredConversationControl(binding=binding, created=False),
    )
    deps = _BindingDeps({owner.jid: owner})
    with (
        patch(
            "pynchy.host.orchestrator.scheduled_binding.ensure_conversation_workspace",
            AsyncMock(return_value=ensured),
        ),
        pytest.raises(ScheduledTaskTerminalError, match="terminal conversation"),
    ):
        await ensure_scheduled_task_binding(task, deps)


async def test_conversation_open_check_accepts_an_open_routed_task(tmp_path) -> None:
    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    owner = _profile()
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("provider:tenant:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder(owner.folder),
    )
    task = replace(_task(), conversation_id=str(conversation.id))
    await create_task(task)

    await ensure_scheduled_task_conversation_open(task, _BindingDeps({owner.jid: owner}))
