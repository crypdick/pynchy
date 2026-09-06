"""Scheduled work must own one durable child-thread runtime before execution."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from conftest import configure_workspace_placement_for, make_settings
from linear_webhook_test_support import DiscordThreadChannel

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.config.api import BuiltinTool, ProfileConfig, WorkspaceConfig
from pynchy.conversation.models import (
    Conversation,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.git_ops.api import resolve_repos_for_group
from pynchy.host.orchestrator.conversation_control import (
    ConversationControlRequest,
    ConversationWorkspaceContext,
    EnsuredConversationWorkspace,
    ensure_conversation_workspace,
)
from pynchy.host.orchestrator.scheduled_binding import (
    ScheduledTaskOwnershipError,
    ScheduledTaskTerminalError,
    ensure_scheduled_task_binding,
    ensure_scheduled_task_conversation_open,
    resolve_scheduled_group,
)
from pynchy.host.orchestrator.threads import EnsuredThread
from pynchy.host.orchestrator.webhook_terminal_retirement import retire_terminal_runtime
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspacePolicy,
    clear_runtime_workspace_policies,
    load_resolved_config,
    register_runtime_workspace_policy,
)
from pynchy.identifiers import (
    GroupFolder,
    SessionId,
)
from pynchy.plugins.api import WebhookConversation, WebhookEvent
from pynchy.plugins.integrations.linear_webhook_effects import process_linear_webhook_event
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    apply_conversation_control_state,
    begin_in_flight_turn,
    cancel_task_and_checkpoint,
    conversation_control_state_matches,
    create_task,
    create_task_if_absent,
    get_conversation,
    get_conversation_for_subject,
    get_in_flight_turn_for_task,
    get_task_by_id,
    init_test_database,
    resolve_conversation,
    retire_conversation_for_terminal,
    set_workspace_profile,
    update_task,
)
from pynchy.workspace.api import (
    CapabilityRule,
    WorkspaceProfile,
)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()
    clear_runtime_workspace_policies()
    yield
    clear_runtime_workspace_policies()


def _profile(*, jid: str = "discord:channel:parent", folder: str = "owner") -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=jid,
        name="Owner",
        folder=folder,
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )


def _task() -> ScheduledTask:
    return ScheduledTask(
        id="task-1",
        group_folder="owner",
        chat_jid="discord:channel:parent",
        prompt="Run the durable job",
        schedule_type="cron",
        schedule_value="0 9 * * *",
        session_policy=SessionPolicy.RESET_BEFORE_RUN,
        status="active",
    )


@dataclass
class _BindingDeps:
    workspaces: dict[str, WorkspaceProfile]
    channels: list = field(default_factory=list)
    ensured_jid: str | None = "discord:channel:scheduled-task"
    ensured: list[tuple[str, str]] = field(default_factory=list)
    scheduled_task_updates: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def ensure_thread(
        self,
        parent_jid: str,
        name: str,
        *,
        participant_ids: tuple[str, ...] = (),
    ) -> EnsuredThread:
        assert participant_ids == ()
        self.ensured.append((parent_jid, name))
        return EnsuredThread(jid=self.ensured_jid, created=len(self.ensured) == 1)

    async def register_workspace(self, profile: WorkspaceProfile) -> None:
        self.workspaces[profile.jid] = profile

    async def unregister_workspace(self, jid: str) -> None:
        self.workspaces.pop(jid, None)

    async def rebind_workspace(self, profile: WorkspaceProfile) -> None:
        for jid, existing in list(self.workspaces.items()):
            if existing.folder == profile.folder:
                self.workspaces.pop(jid)
        self.workspaces[profile.jid] = profile

    async def bind_routed_session(self, group_folder: str, session_id: SessionId) -> None:
        del group_folder, session_id

    async def get_scheduled_conversation(
        self, conversation_id: ConversationId
    ) -> Conversation | None:
        return await get_conversation(conversation_id)

    async def persist_scheduled_task_updates(
        self, task_id: str, updates: dict[str, object]
    ) -> None:
        self.scheduled_task_updates.append((task_id, updates))
        await update_task(task_id, updates)

    async def cancel_scheduled_task(self, task_id: str) -> None:
        await cancel_task_and_checkpoint(task_id)


@dataclass
class _TitleChannel:
    titles: list[tuple[str, str]] = field(default_factory=list)

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:channel:")

    async def set_thread_title(self, child_jid: str, title: str) -> None:
        self.titles.append((child_jid, title))


@dataclass
class _RetirementBindingDeps(_BindingDeps):
    terminal_persisted: asyncio.Event = field(default_factory=asyncio.Event)
    retired_conversations: list[ConversationId] = field(default_factory=list)

    async def conversation_control_state_matches(
        self,
        conversation_id: ConversationId,
        *,
        closed: bool,
        control_state_revision: str | None,
    ) -> bool:
        return await conversation_control_state_matches(
            conversation_id,
            closed=closed,
            control_state_revision=control_state_revision,
        )

    async def retire_conversation_runtime(self, folder: str) -> None:
        del folder

    async def retire_conversation_tasks(self, conversation_id: ConversationId) -> None:
        self.retired_conversations.append(conversation_id)
        await self.cancel_scheduled_task("task-1")


async def test_unnamed_task_cannot_create_a_child_thread() -> None:
    owner = _profile()
    deps = _BindingDeps({owner.jid: owner})
    task = _task()
    await create_task(task)

    with pytest.raises(ScheduledTaskOwnershipError, match="lacks a managed thread name"):
        await ensure_scheduled_task_binding(task, deps)

    assert deps.ensured == []
    persisted = await get_task_by_id(task.id)
    assert persisted is not None
    assert persisted.bound_chat_jid is None
    assert persisted.bound_group_folder is None


async def test_existing_named_task_binding_skips_thread_recreation() -> None:
    owner = _profile()
    bound = _profile(
        jid="discord:channel:scheduled-task",
        folder="owner__thread_discord-channel-scheduled-task",
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

    assert ensured == task
    assert deps.ensured == []
    assert deps.scheduled_task_updates == []


async def test_existing_named_task_binding_reconciles_its_title() -> None:
    owner = _profile()
    bound = _profile(
        jid="discord:channel:scheduled-task",
        folder="owner__thread_discord-channel-scheduled-task",
    )
    bound = replace(bound, name="Owner/owner | durable task")
    task = replace(
        _task(),
        config_job_name="vault-durable-task",
        derived_thread_name="owner | durable task",
        bound_chat_jid=bound.jid,
        bound_group_folder=bound.folder,
    )
    await create_task(task)
    channel = _TitleChannel()
    deps = _BindingDeps({owner.jid: owner, bound.jid: bound}, channels=[channel])

    ensured = await ensure_scheduled_task_binding(task, deps)

    assert ensured.derived_thread_name == "⚙️ durable task"
    assert channel.titles == [(bound.jid, "⚙️ durable task")]
    assert deps.workspaces[bound.jid].name == "Owner/⚙️ durable task"
    assert deps.ensured == []
    assert deps.scheduled_task_updates == [(task.id, {"derived_thread_name": "⚙️ durable task"})]


async def test_existing_named_task_binding_keeps_matching_profile_name() -> None:
    owner = _profile()
    bound = _profile(
        jid="discord:channel:scheduled-task",
        folder="owner__thread_discord-channel-scheduled-task",
    )
    task = replace(
        _task(),
        config_job_name="vault-durable-task",
        derived_thread_name="⚙️ durable task",
        bound_chat_jid=bound.jid,
        bound_group_folder=bound.folder,
    )
    bound = replace(bound, name="Owner/⚙️ durable task")
    await create_task(task)
    deps = _BindingDeps({owner.jid: owner, bound.jid: bound}, channels=[_TitleChannel()])

    ensured = await ensure_scheduled_task_binding(task, deps)

    assert ensured == task
    assert deps.scheduled_task_updates == []


async def test_task_without_workspace_owner_fails_before_execution(tmp_path) -> None:
    task = _task()
    deps = _BindingDeps({})

    configure_workspace_placement_for(make_settings(groups_dir=tmp_path))
    with pytest.raises(ScheduledTaskOwnershipError, match="owner workspace is unavailable"):
        await ensure_scheduled_task_binding(task, deps)


async def test_terminal_routed_task_cancels_task_and_checkpoint_before_binding() -> None:
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:project:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("owner"),
    )
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision=None,
    )
    task = replace(
        _task(),
        conversation_id=str(conversation.id),
        next_run="2026-07-27T05:00:00+00:00",
    )
    assert await create_task_if_absent(task)
    before = await get_task_by_id(task.id)
    assert before is not None
    assert before.next_run == task.next_run
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="terminal-task-turn",
            chat_jid=task.chat_jid,
            group_folder=task.group_folder,
            work_kind=InFlightWorkKind.SCHEDULED,
            input_messages=[],
            input_start_cursor="",
            input_end_cursor="",
            started_at="2026-07-27T04:00:00+00:00",
            task_id=task.id,
        )
    )

    with pytest.raises(ScheduledTaskTerminalError, match="terminal conversation"):
        await ensure_scheduled_task_binding(task, _BindingDeps({}))

    persisted = await get_task_by_id(task.id)
    assert persisted is not None
    assert persisted.status == "cancelled"
    assert persisted.next_run is None
    assert await get_in_flight_turn_for_task(task.id) is None


async def test_routed_binding_rejects_terminal_intent_after_workspace_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _profile()
    await set_workspace_profile(owner)
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:project:issue"),
            key=ConversationSubjectKey("issue-race"),
        ),
        GroupFolder(owner.folder),
    )
    task = replace(
        _task(),
        conversation_id=str(conversation.id),
        derived_thread_name="[SYN-1] Routed binding race",
    )
    await create_task(task)
    deps = _RetirementBindingDeps(
        {owner.jid: owner},
        channels=[DiscordThreadChannel()],
    )
    workspace_registered = asyncio.Event()
    original_ensure = ensure_conversation_workspace

    async def pause_after_workspace_registration(
        context: ConversationWorkspaceContext,
        request: ConversationControlRequest,
    ) -> EnsuredConversationWorkspace:
        ensured = await original_ensure(context, request)
        workspace_registered.set()
        await deps.terminal_persisted.wait()
        return ensured

    monkeypatch.setattr(
        "pynchy.host.orchestrator.scheduled_binding.ensure_conversation_workspace",
        pause_after_workspace_registration,
    )
    binding = asyncio.create_task(ensure_scheduled_task_binding(task, deps))
    terminal: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(workspace_registered.wait(), timeout=1)
        identity = ExternalDeliveryIdentity(
            provider=ExternalProvider("linear"),
            route=ExternalRoute("project"),
            delivery_id=ExternalDeliveryId("terminal-binding-race"),
        )
        retirement = await retire_conversation_for_terminal(
            conversation.id,
            preserve_delivery=identity,
            control_state_revision="2026-07-27T00:00:01+00:00",
        )
        deps.terminal_persisted.set()
        terminal = asyncio.create_task(
            retire_terminal_runtime(
                deps,
                conversation.id,
                retirement,
                set(),
            )
        )
        await asyncio.wait_for(deps.terminal_persisted.wait(), timeout=1)

        with pytest.raises(ScheduledTaskTerminalError, match="terminal conversation"):
            await binding
        await terminal

        persisted = await get_task_by_id(task.id)
        assert persisted is not None
        assert persisted.status == "cancelled"
        assert deps.retired_conversations == [conversation.id]
        assert all(
            profile.folder != routed_conversation_folder(owner.folder, conversation.id)
            for profile in deps.workspaces.values()
        )
    finally:
        if not binding.done():
            binding.cancel()
        if terminal is not None and not terminal.done():
            await terminal


async def test_existing_linear_task_is_migrated_to_continue_before_execution() -> None:
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:project:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("owner"),
    )
    owner = _profile()
    routed = _profile(
        jid="discord:channel:linear-thread",
        folder="owner__thread_conversation-conv-1",
    )
    deps = _BindingDeps({owner.jid: owner, routed.jid: routed})
    task = replace(
        _task(),
        input_source="external:linear:human_approved",
        conversation_id=str(conversation.id),
    )
    await create_task(task)

    with patch(
        "pynchy.host.orchestrator.scheduled_binding._bind_routed_conversation",
        new_callable=AsyncMock,
        return_value=(routed, "[SYN-89] Durable runtime"),
    ):
        bound = await ensure_scheduled_task_binding(task, deps)

    assert bound.session_policy is SessionPolicy.CONTINUE
    persisted = await get_task_by_id(task.id)
    assert persisted is not None
    assert persisted.session_policy is SessionPolicy.CONTINUE
    assert deps.scheduled_task_updates[0] == (
        task.id,
        {"session_policy": SessionPolicy.CONTINUE},
    )


async def test_scheduled_linear_binding_restores_webhook_conversation_repo_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        tools={
            "repo_read": BuiltinTool(type="builtin"),
            "repo_write": BuiltinTool(type="builtin"),
        },
        profiles={
            "owner": ProfileConfig(
                repo="crypdick/pynchy",
                tools=["repo_read", "repo_write"],
                permissions={"allow": ["repo.write"]},
            )
        },
        workspaces={"owner": WorkspaceConfig(profiles=["owner"])},
    )
    monkeypatch.setattr("pynchy.config.settings._settings", settings)
    owner = _profile()
    await set_workspace_profile(owner)
    subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:synapse:issue"),
        key=ConversationSubjectKey("issue-1"),
    )
    event = WebhookEvent(
        delivery_id="delivery-1",
        event_type="Issue",
        action="update",
        subject_id="issue-1",
        occurred_at=datetime.now(UTC).isoformat(),
        instructions="Execute the approved issue.",
        external_context={"identifier": "SYN-1"},
        conversation=WebhookConversation(
            subject=subject,
            control_title="[SYN-1] Routed policy",
            workspace=owner.folder,
        ),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects._controller_owns_event",
        AsyncMock(return_value=True),
    )

    processed = await process_linear_webhook_event(event)
    conversation = await get_conversation_for_subject(subject)

    assert processed.conversation is not None
    assert conversation is not None
    folder = routed_conversation_folder(conversation.workspace, conversation.id)
    assert load_resolved_config(folder) is None

    task = replace(
        _task(),
        input_source="trusted:linear:authorized",
        conversation_id=str(conversation.id),
        derived_thread_name="[SYN-1] Routed policy",
    )
    await create_task(task)
    deps = _BindingDeps(
        {owner.jid: owner},
        channels=[DiscordThreadChannel()],
    )

    bound = await ensure_scheduled_task_binding(task, deps)

    assert bound.repo_access is None
    assert bound.bound_group_folder == folder
    resolved = load_resolved_config(folder)
    assert resolved is not None
    assert resolved.repo == ["crypdick/pynchy"]
    assert [repo.slug for repo in resolve_repos_for_group(folder)] == ["crypdick/pynchy"]

    clear_runtime_workspace_policies()
    register_runtime_workspace_policy(
        folder,
        RuntimeWorkspacePolicy(
            parent_workspace=owner.folder,
            tools=("repo_read",),
            capabilities={"repo.write": CapabilityRule(decision="deny")},
        ),
    )

    await ensure_scheduled_task_binding(bound, deps)

    narrowed = load_resolved_config(folder)
    assert narrowed is not None
    assert narrowed.tools == ["repo_read"]
    assert narrowed.capabilities["repo.write"].decision == "deny"


async def test_linear_task_without_conversation_fails_before_execution() -> None:
    owner = _profile()
    task = replace(
        _task(),
        input_source="external:linear:human_approved",
        prompt='{"issue_id": "SYN-89"}',
    )
    await create_task(task)

    with pytest.raises(
        ScheduledTaskOwnershipError,
        match="no durable issue conversation",
    ):
        await ensure_scheduled_task_binding(task, _BindingDeps({owner.jid: owner}))


def test_resolve_scheduled_group_finds_exact_owner_and_rejects_unknown_folder() -> None:
    owner = _profile()

    assert resolve_scheduled_group({owner.jid: owner}, owner.folder) == owner
    assert resolve_scheduled_group({owner.jid: owner}, "missing") is None


async def test_existing_bound_child_without_owner_cannot_be_reused() -> None:
    bound = _profile(
        jid="discord:channel:scheduled-task",
        folder="owner__thread_discord-channel-scheduled-task",
    )
    task = replace(
        _task(),
        derived_thread_name="Owner | durable task",
        bound_chat_jid=bound.jid,
        bound_group_folder=bound.folder,
    )
    await create_task(task)

    with pytest.raises(ScheduledTaskOwnershipError, match="owner workspace is unavailable"):
        await ensure_scheduled_task_binding(task, _BindingDeps({bound.jid: bound}))


async def test_named_binding_rebinds_a_stale_profile_with_the_same_folder() -> None:
    owner = _profile()
    stale = _profile(
        jid="discord:channel:stale",
        folder="owner__thread_discord-channel-scheduled-task",
    )
    task = replace(_task(), derived_thread_name="Owner | durable task")
    await create_task(task)
    deps = _BindingDeps({owner.jid: owner, stale.jid: stale})

    bound = await ensure_scheduled_task_binding(task, deps)

    assert bound.bound_chat_jid == deps.ensured_jid
    assert stale.jid not in deps.workspaces
    assert bound.bound_chat_jid is not None
    assert deps.workspaces[bound.bound_chat_jid].folder == bound.bound_group_folder


async def test_named_binding_rejects_thread_creation_without_a_chat_jid() -> None:
    owner = _profile()
    task = replace(_task(), derived_thread_name="Owner | durable task")
    await create_task(task)

    with pytest.raises(ScheduledTaskOwnershipError, match="no chat JID"):
        await ensure_scheduled_task_binding(
            task,
            _BindingDeps({owner.jid: owner}, ensured_jid=None),
        )


async def test_routed_binding_rejects_a_missing_conversation() -> None:
    owner = _profile()
    task = replace(
        _task(),
        conversation_id="conversation-missing",
        derived_thread_name="Owner | durable task",
    )
    await create_task(task)

    with pytest.raises(ScheduledTaskOwnershipError, match="missing conversation"):
        await ensure_scheduled_task_binding(task, _BindingDeps({owner.jid: owner}))


async def test_conversation_open_check_is_a_no_op_for_named_tasks() -> None:
    await ensure_scheduled_task_conversation_open(_task(), _BindingDeps({}))
