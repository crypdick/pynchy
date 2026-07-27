"""Scheduled work must own one durable child-thread runtime before execution."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings
from linear_webhook_test_support import DiscordThreadChannel

from pynchy.config import WorkspaceConfig
from pynchy.config.models import ProfileConfig
from pynchy.conversation.models import (
    Conversation,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.host.git_ops.repo import resolve_repos_for_group
from pynchy.host.orchestrator.scheduled_binding import (
    ScheduledTaskOwnershipError,
    ensure_scheduled_task_binding,
)
from pynchy.host.orchestrator.threads import EnsuredThread
from pynchy.host.orchestrator.workspace_config import (
    RuntimeWorkspaceRestriction,
    clear_runtime_workspace_restrictions,
    load_resolved_config,
    register_runtime_workspace_restriction,
)
from pynchy.plugins.integrations.linear_webhook_effects import process_linear_webhook_event
from pynchy.plugins.webhooks import WebhookConversation, WebhookEvent
from pynchy.state import (
    create_task,
    get_conversation,
    get_conversation_for_subject,
    get_task_by_id,
    init_test_database,
    set_workspace_profile,
    update_task,
)
from pynchy.types import CapabilityRule, ScheduledTask, SessionId, SessionPolicy, WorkspaceProfile


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()
    clear_runtime_workspace_restrictions()
    yield
    clear_runtime_workspace_restrictions()


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
    ensured_jid: str = "discord:channel:scheduled-task"
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


async def test_unnamed_task_gets_persistent_child_thread_binding(tmp_path) -> None:
    owner = _profile()
    deps = _BindingDeps({owner.jid: owner})
    task = _task()
    await create_task(task)

    with patch(
        "pynchy.host.orchestrator.workspace_placement.get_settings",
        return_value=make_settings(groups_dir=tmp_path),
    ):
        bound = await ensure_scheduled_task_binding(task, deps)
        rebound = await ensure_scheduled_task_binding(bound, deps)

    assert bound.bound_chat_jid == deps.ensured_jid
    assert bound.bound_chat_jid != owner.jid
    assert bound.bound_group_folder is not None
    assert bound.bound_group_folder != owner.folder
    assert rebound.bound_chat_jid == bound.bound_chat_jid
    assert {parent for parent, _title in deps.ensured} == {owner.jid}
    persisted = await get_task_by_id(task.id)
    assert persisted is not None
    assert persisted.bound_chat_jid == bound.bound_chat_jid
    assert persisted.bound_group_folder == bound.bound_group_folder


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


async def test_task_without_workspace_owner_fails_before_execution(tmp_path) -> None:
    task = _task()
    deps = _BindingDeps({})

    with (
        patch(
            "pynchy.host.orchestrator.workspace_placement.get_settings",
            return_value=make_settings(groups_dir=tmp_path),
        ),
        pytest.raises(ScheduledTaskOwnershipError, match="owner workspace is unavailable"),
    ):
        await ensure_scheduled_task_binding(task, deps)


async def test_existing_linear_task_is_migrated_to_continue_before_execution() -> None:
    owner = _profile()
    routed = _profile(
        jid="discord:channel:linear-thread",
        folder="owner__thread_conversation-conv-1",
    )
    deps = _BindingDeps({owner.jid: owner, routed.jid: routed})
    task = replace(
        _task(),
        input_source="external:linear:human_approved",
        conversation_id="conv-1",
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
        profiles={
            "owner": ProfileConfig(
                repo="crypdick/pynchy",
                tools=["repo_read", "repo_write"],
                capabilities={"repo.write": {"decision": "allow"}},
            )
        },
        workspaces={"owner": WorkspaceConfig(profiles=["owner"])},
    )
    monkeypatch.setattr("pynchy.config.settings._state.settings", settings)
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

    assert processed.conversation is None
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

    clear_runtime_workspace_restrictions()
    register_runtime_workspace_restriction(
        folder,
        RuntimeWorkspaceRestriction(
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
