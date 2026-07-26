"""Scheduled work must own one durable child-thread runtime before execution."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from conftest import make_settings

from pynchy.host.orchestrator.scheduled_binding import (
    ScheduledTaskOwnershipError,
    ensure_scheduled_task_binding,
)
from pynchy.host.orchestrator.threads import EnsuredThread
from pynchy.state import create_task, get_task_by_id, init_test_database
from pynchy.types import ScheduledTask, SessionId, SessionPolicy, WorkspaceProfile


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


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
