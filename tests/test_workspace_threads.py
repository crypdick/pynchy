"""Tests for declared child threads and guarded legacy-workspace retirement."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import init_test_database, make_settings

import pynchy.host.orchestrator.workspace_config as workspace_config
import pynchy.host.orchestrator.workspace_threads as workspace_threads
from pynchy.config.api import (
    ProfileConfig,
    WorkspaceConfig,
    WorkspaceMigrationConfig,
    WorkspaceThreadConfig,
    dynamic_thread_folder,
)
from pynchy.host.orchestrator.threads import (
    create_thread,
    ensure_thread,
    find_thread,
    set_thread_closed,
    supports_thread_creation,
)
from pynchy.host.orchestrator.workspace_config import reconcile_workspaces
from pynchy.host.orchestrator.workspace_threads import (
    WorkspaceThreadAction,
    reconcile_workspace_threads,
)
from pynchy.plugins.api import (
    InboundFetchResult,
    OutboundEvent,
)
from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import create_task
from pynchy.workspace.api import WorkspaceProfile


class _ThreadChannel:
    name = "connection.discord.main"
    formatter = object()

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = existing or {}
        self.created: list[tuple[str, str]] = []
        self.kinds: list[tuple[str, str]] = []

    async def connect(self) -> None: ...

    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid: str) -> bool:
        return jid.startswith("discord:channel:")

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult:
        return InboundFetchResult(messages=[])

    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        assert parent_jid == "discord:channel:relationships"
        return self.existing.get(name)

    async def create_thread(
        self, parent_jid: str, name: str, *, participant_ids: tuple[str, ...] = ()
    ) -> str:
        assert parent_jid == "discord:channel:relationships"
        assert participant_ids == ()
        self.created.append((parent_jid, name))
        return f"discord:channel:new-{name}"

    async def set_thread_kind(self, child_jid: str, kind: str) -> None:
        self.kinds.append((child_jid, kind))


class _CreationOnlyChannel(_ThreadChannel):
    find_thread = None  # type: ignore[assignment]


class _FailingLookupChannel(_ThreadChannel):
    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        del parent_jid, name
        raise RuntimeError("Discord unavailable")


class _EmptyThreadChannel(_ThreadChannel):
    async def create_thread(
        self, parent_jid: str, name: str, *, participant_ids: tuple[str, ...] = ()
    ) -> str:
        del parent_jid, name, participant_ids
        return ""


def _parent() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:channel:relationships",
        name="Relationships",
        folder="relationships",
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_thread_capabilities_fail_closed_without_provider_support() -> None:
    parent_jid = "discord:channel:relationships"

    assert await supports_thread_creation([], parent_jid) is False
    assert await find_thread([], parent_jid, "family") is None
    with pytest.raises(RuntimeError, match="thread creation"):
        await create_thread([], parent_jid, "family")
    with pytest.raises(RuntimeError, match="thread lifecycle"):
        await set_thread_closed([], "discord:channel:family", closed=True)
    with pytest.raises(RuntimeError, match="thread lookup"):
        await ensure_thread([], parent_jid, "family")


@pytest.mark.asyncio
async def test_thread_creation_rejects_provider_without_a_child_jid() -> None:
    with pytest.raises(RuntimeError, match="no JID"):
        await create_thread([_EmptyThreadChannel()], "discord:channel:relationships", "family")


@pytest.mark.asyncio
async def test_reconciles_declared_threads_by_reusing_or_creating_them() -> None:
    parent = _parent()
    workspaces = {parent.jid: parent}
    channel = _ThreadChannel({"family": "discord:channel:family"})

    register = AsyncMock(side_effect=lambda profile: workspaces.update({profile.jid: profile}))

    actions = await reconcile_workspace_threads(
        workspaces,
        {
            "relationships": WorkspaceConfig(
                threads=[
                    WorkspaceThreadConfig(name="family"),
                    WorkspaceThreadConfig(name="family-gardening", kind="automation"),
                ]
            )
        },
        [channel],
        register,
    )

    assert channel.created == [
        ("discord:channel:relationships", "family-gardening"),
    ]
    assert channel.kinds == [
        ("discord:channel:family", "topic"),
        ("discord:channel:new-family-gardening", "automation"),
    ]
    assert [action.operation for action in actions] == [
        "reuse",
        "register",
        "create",
        "register",
    ]
    assert workspaces["discord:channel:family"].name == "Relationships/family"
    assert workspaces["discord:channel:new-family-gardening"].folder.startswith(
        "relationships__thread_"
    )


@pytest.mark.asyncio
async def test_dry_run_reports_creation_without_mutating_threads_or_workspaces() -> None:
    parent = _parent()
    workspaces = {parent.jid: parent}
    channel = _ThreadChannel()
    register = AsyncMock()

    actions = await reconcile_workspace_threads(
        workspaces,
        {"relationships": WorkspaceConfig(threads=[WorkspaceThreadConfig(name="family")])},
        [channel],
        register,
        dry_run=True,
    )

    assert actions == [WorkspaceThreadAction("create", "relationships", "family")]
    assert channel.created == []
    register.assert_not_awaited()
    assert workspaces == {parent.jid: parent}


@pytest.mark.asyncio
async def test_missing_parent_waits_before_looking_up_or_creating_a_thread() -> None:
    actions = await reconcile_workspace_threads(
        {},
        {"relationships": WorkspaceConfig(threads=[WorkspaceThreadConfig(name="family")])},
        [],
        AsyncMock(),
    )

    assert actions == [WorkspaceThreadAction("await_parent", "relationships", "family")]


@pytest.mark.asyncio
async def test_declared_child_workspace_uses_its_resolved_policy(monkeypatch) -> None:
    settings = make_settings(
        profiles={"child": ProfileConfig(is_admin=True)},
        workspaces={"family-space": WorkspaceConfig(profiles=["child"])},
    )
    monkeypatch.setattr(workspace_threads, "get_settings", lambda: settings)
    parent = _parent()
    child_jid = "discord:channel:family"
    channel = _ThreadChannel({"family": child_jid})
    workspaces = {parent.jid: parent}
    register = AsyncMock(side_effect=lambda profile: workspaces.update({profile.jid: profile}))

    actions = await reconcile_workspace_threads(
        workspaces,
        {
            "relationships": WorkspaceConfig(
                threads=[
                    WorkspaceThreadConfig(
                        name="family", workspace="family-space", profiles=["child"]
                    )
                ]
            )
        },
        [channel],
        register,
    )

    assert actions == [
        WorkspaceThreadAction("reuse", "relationships", "family", child_jid),
        WorkspaceThreadAction("register", "relationships", "family", child_jid),
    ]
    child = workspaces[child_jid]
    assert child.folder == "family-space"
    assert child.name == "Family Space"
    assert child.is_admin is True


@pytest.mark.asyncio
async def test_dry_run_reports_registration_for_existing_stale_child() -> None:
    parent = _parent()
    child_jid = "discord:channel:family"
    stale = WorkspaceProfile(
        jid=child_jid,
        name="Old Family",
        folder="old-family",
        trigger=parent.trigger,
        added_at="2024-01-01T00:00:00+00:00",
    )
    channel = _ThreadChannel({"family": child_jid})
    register = AsyncMock()

    actions = await reconcile_workspace_threads(
        {parent.jid: parent, child_jid: stale},
        {"relationships": WorkspaceConfig(threads=[WorkspaceThreadConfig(name="family")])},
        [channel],
        register,
        dry_run=True,
    )

    assert actions == [
        WorkspaceThreadAction("reuse", "relationships", "family", child_jid),
        WorkspaceThreadAction("register", "relationships", "family", child_jid),
    ]
    register.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_matching_child_is_reused_without_registration() -> None:
    parent = _parent()
    child_jid = "discord:channel:family"
    existing = WorkspaceProfile(
        jid=child_jid,
        name="Relationships/family",
        folder=dynamic_thread_folder(parent.folder, child_jid),
        trigger=parent.trigger,
        security=parent.security,
        is_admin=parent.is_admin,
        added_at="2024-01-01T00:00:00+00:00",
    )
    channel = _ThreadChannel({"family": child_jid})
    register = AsyncMock()

    actions = await reconcile_workspace_threads(
        {parent.jid: parent, child_jid: existing},
        {"relationships": WorkspaceConfig(threads=[WorkspaceThreadConfig(name="family")])},
        [channel],
        register,
    )

    assert actions == [WorkspaceThreadAction("reuse", "relationships", "family", child_jid)]
    register.assert_not_awaited()


@pytest.mark.asyncio
async def test_refuses_thread_creation_without_idempotent_lookup() -> None:
    parent = _parent()
    channel = _CreationOnlyChannel()

    actions = await reconcile_workspace_threads(
        {parent.jid: parent},
        {"relationships": WorkspaceConfig(threads=[WorkspaceThreadConfig(name="family")])},
        [channel],
        AsyncMock(),
    )

    assert actions == [
        WorkspaceThreadAction(
            "blocked",
            "relationships",
            "family",
            detail="owning channel cannot look up child threads",
        )
    ]
    assert channel.created == []


@pytest.mark.asyncio
async def test_thread_lookup_failure_does_not_block_workspace_reconciliation() -> None:
    parent = _parent()
    channel = _FailingLookupChannel()

    actions = await reconcile_workspace_threads(
        {parent.jid: parent},
        {"relationships": WorkspaceConfig(threads=[WorkspaceThreadConfig(name="family")])},
        [channel],
        AsyncMock(),
    )

    assert actions == [
        WorkspaceThreadAction(
            "blocked",
            "relationships",
            "family",
            detail="thread ensure failed: RuntimeError",
        )
    ]
    assert channel.created == []


@pytest.mark.asyncio
async def test_conflicting_child_registration_is_blocked_without_stopping_reconciliation() -> None:
    parent = _parent()
    conflicting_jid = "discord:channel:marketplace-inbox"
    foreign = WorkspaceProfile(
        jid=conflicting_jid,
        name="Marketplace Inbox Poller",
        folder="marketplace-inbox-poller",
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )
    workspaces = {parent.jid: parent, foreign.jid: foreign}
    available_jid = "discord:channel:family"
    channel = _ThreadChannel(
        {
            "marketplace-inbox": conflicting_jid,
            "family": available_jid,
        }
    )

    def register(profile: WorkspaceProfile) -> None:
        if profile.jid == conflicting_jid:
            raise ValueError(
                "Chat JID 'discord:channel:marketplace-inbox' is already owned by "
                "workspace 'marketplace-inbox-poller'"
            )
        workspaces[profile.jid] = profile

    register_fn = AsyncMock(side_effect=register)
    actions = await reconcile_workspace_threads(
        workspaces,
        {
            "relationships": WorkspaceConfig(
                threads=[
                    WorkspaceThreadConfig(name="marketplace-inbox"),
                    WorkspaceThreadConfig(name="family"),
                ]
            )
        },
        [channel],
        register_fn,
    )

    assert actions == [
        WorkspaceThreadAction("reuse", "relationships", "marketplace-inbox", conflicting_jid),
        WorkspaceThreadAction(
            "blocked",
            "relationships",
            "marketplace-inbox",
            conflicting_jid,
            "workspace registration failed: ValueError",
        ),
        WorkspaceThreadAction("reuse", "relationships", "family", available_jid),
        WorkspaceThreadAction("register", "relationships", "family", available_jid),
    ]
    assert workspaces[conflicting_jid] == foreign
    assert workspaces[available_jid].folder == dynamic_thread_folder("relationships", available_jid)
    assert register_fn.await_count == 2


@pytest.mark.asyncio
async def test_unretired_legacy_workspace_remains_registered(monkeypatch, tmp_path) -> None:
    await init_test_database()
    settings = make_settings(
        profiles={"relationship": ProfileConfig()},
        workspaces={
            "relationships": WorkspaceConfig(
                profiles=["relationship"],
                threads=[WorkspaceThreadConfig(name="family")],
            )
        },
        workspace_migrations={
            "fam": WorkspaceMigrationConfig(
                target_workspace="relationships",
                target_thread="family",
            )
        },
        groups_dir=tmp_path / "groups",
    )
    monkeypatch.setattr(workspace_config, "get_settings", lambda: settings)
    old_root = WorkspaceProfile(
        jid="discord:channel:fam",
        name="Fam",
        folder="fam",
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )
    parent = _parent()
    registered = {old_root.jid: old_root, parent.jid: parent}
    unregister = AsyncMock()

    await reconcile_workspaces(registered, [], AsyncMock(), unregister_fn=unregister)

    unregister.assert_not_awaited()
    assert old_root.jid in registered


@pytest.mark.asyncio
async def test_active_legacy_scheduled_task_blocks_retirement(monkeypatch, tmp_path) -> None:
    await init_test_database()
    settings = make_settings(
        profiles={"relationship": ProfileConfig()},
        workspaces={
            "relationships": WorkspaceConfig(
                profiles=["relationship"],
                threads=[WorkspaceThreadConfig(name="family")],
            )
        },
        workspace_migrations={
            "fam": WorkspaceMigrationConfig(
                target_workspace="relationships",
                target_thread="family",
                inbound_retargeted=True,
                scheduled_jobs_retargeted=True,
                retire_legacy_workspace=True,
            )
        },
        groups_dir=tmp_path / "groups",
    )
    monkeypatch.setattr(workspace_config, "get_settings", lambda: settings)
    await create_task(
        ScheduledTask(
            id="legacy-task",
            group_folder="fam",
            chat_jid="discord:channel:fam",
            prompt="Do the old task.",
            schedule_type="cron",
            schedule_value="0 9 * * *",
            session_policy=SessionPolicy.CONTINUE,
            next_run=None,
            status="active",
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    old_root = WorkspaceProfile(
        jid="discord:channel:fam",
        name="Fam",
        folder="fam",
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )
    parent = _parent()
    registered = {old_root.jid: old_root, parent.jid: parent}
    unregister = AsyncMock()

    await reconcile_workspaces(registered, [], AsyncMock(), unregister_fn=unregister)

    unregister.assert_not_awaited()
    assert old_root.jid in registered


@pytest.mark.asyncio
async def test_confirmed_admin_legacy_workspace_and_child_thread_retire(
    monkeypatch, tmp_path
) -> None:
    await init_test_database()
    settings = make_settings(
        profiles={"relationship": ProfileConfig()},
        workspaces={
            "relationships": WorkspaceConfig(
                profiles=["relationship"],
                threads=[WorkspaceThreadConfig(name="family")],
            )
        },
        workspace_migrations={
            "discord-admin": WorkspaceMigrationConfig(
                target_workspace="relationships",
                target_thread="family",
                inbound_retargeted=True,
                scheduled_jobs_retargeted=True,
                retire_legacy_workspace=True,
            )
        },
        groups_dir=tmp_path / "groups",
    )
    monkeypatch.setattr(workspace_config, "get_settings", lambda: settings)
    old_root = WorkspaceProfile(
        jid="discord:channel:discord-admin",
        name="Discord Admin",
        folder="discord-admin",
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
        is_admin=True,
    )
    old_child = WorkspaceProfile(
        jid="discord:channel:old-child",
        name="Discord Admin/old-child",
        folder=dynamic_thread_folder("discord-admin", "discord:channel:old-child"),
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
        is_admin=True,
    )
    parent = _parent()
    registered = {
        old_root.jid: old_root,
        old_child.jid: old_child,
        parent.jid: parent,
    }
    unregister = AsyncMock()

    await reconcile_workspaces(registered, [], AsyncMock(), unregister_fn=unregister)

    assert {call.args[0] for call in unregister.await_args_list} == {
        old_root.jid,
        old_child.jid,
    }
