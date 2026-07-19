"""Tests for declared child threads and guarded legacy-workspace retirement."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import init_test_database, make_settings

import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.config.models import (
    ProfileConfig,
    WorkspaceConfig,
    WorkspaceThreadConfig,
)
from pynchy.config.workspace_layout import WorkspaceMigrationConfig
from pynchy.host.orchestrator.workspace_config import reconcile_workspaces
from pynchy.host.orchestrator.workspace_threads import (
    WorkspaceThreadAction,
    reconcile_workspace_threads,
)
from pynchy.state import create_task
from pynchy.types import InboundFetchResult, OutboundEvent, ScheduledTask, WorkspaceProfile


class _ThreadChannel:
    name = "connection.discord.main"
    formatter = object()

    def __init__(self, existing: dict[str, str] | None = None) -> None:
        self.existing = existing or {}
        self.created: list[tuple[str, str]] = []

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


class _CreationOnlyChannel(_ThreadChannel):
    find_thread = None  # type: ignore[assignment]


class _FailingLookupChannel(_ThreadChannel):
    async def find_thread(self, parent_jid: str, name: str) -> str | None:
        del parent_jid, name
        raise RuntimeError("Discord unavailable")


def _parent() -> WorkspaceProfile:
    return WorkspaceProfile(
        jid="discord:channel:relationships",
        name="Relationships",
        folder="relationships",
        trigger="@Pynchy",
        added_at=datetime.now(UTC).isoformat(),
    )


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
                    WorkspaceThreadConfig(name="family-gardening"),
                ]
            )
        },
        [channel],
        register,
    )

    assert channel.created == [
        ("discord:channel:relationships", "family-gardening"),
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
            context_mode="group",
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
