"""Tests for the database layer."""

from __future__ import annotations

from dataclasses import replace

import pytest

from pynchy.scheduling.api import (
    ScheduledTask,
    SessionPolicy,
)
from pynchy.state import (
    begin_work_item_transition,
    bind_work_item_execution_to_task,
    bind_work_item_execution_to_turn,
    cancel_work_item_execution,
    create_host_job,
    create_task,
    create_work_item_claim,
    get_all_workspace_profiles,
    get_chat_history,
    get_host_job_by_id,
    get_last_group_sync,
    get_task_by_id,
    get_work_item_transition_by_request,
    get_workspace_profile,
    rebind_workspace_profile,
    resolve_work_item_transition,
    set_last_group_sync,
    set_workspace_profile,
    set_workspace_profiles,
    store_chat_metadata,
    update_host_job,
    update_task,
)
from pynchy.state.connection import atomic_write
from pynchy.work_items.api import (
    WorkItemClaimRequest,
    WorkItemExecutionStatus,
    WorkItemTransitionRequest,
    WorkItemTransitionStatus,
)
from pynchy.workspace.api import (
    CapabilityRule,
    ContainerConfig,
    ServiceTrustConfig,
    WorkspaceProfile,
    WorkspaceSecurity,
)
from tests.state_support import (
    _assert_full_task,
    _full_task,
    _store,
    _store_message_row,
)

pytest_plugins = ("tests.state_support",)


class TestWorkspaceProfiles:
    async def test_set_and_get_workspace_profile(self):
        profile = WorkspaceProfile(
            jid="test@g.us",
            name="Test Workspace",
            folder="test-ws",
            trigger="@Test",
            container_config=ContainerConfig(timeout=90),
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        result = await get_workspace_profile("test@g.us")
        assert result is not None
        assert result.name == "Test Workspace"
        assert result.folder == "test-ws"
        assert result.trigger == "@Test"
        assert result.container_config is not None
        assert result.container_config.timeout == 90

    async def test_workspace_profile_with_security(self):
        security = WorkspaceSecurity(
            services={
                "email": ServiceTrustConfig(
                    public_source=True,
                    secret_data=True,
                    public_sink=True,
                    dangerous_writes=True,
                ),
                "calendar": ServiceTrustConfig(
                    public_source=False,
                    secret_data=False,
                    public_sink=False,
                    dangerous_writes=False,
                ),
            },
            contains_secrets=True,
            cop_active=False,
            capabilities={"repo.write": CapabilityRule(decision="needs_human")},
        )
        profile = WorkspaceProfile(
            jid="secure@g.us",
            name="Secure Workspace",
            folder="secure-ws",
            trigger="@Secure",
            security=security,
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        result = await get_workspace_profile("secure@g.us")
        assert result is not None
        assert result.security.contains_secrets is True
        assert result.security.cop_active is False
        assert result.security.capabilities["repo.write"].decision == "needs_human"
        assert "email" in result.security.services
        assert result.security.services["email"].public_source is True
        assert result.security.services["email"].dangerous_writes is True
        assert "calendar" in result.security.services
        assert result.security.services["calendar"].public_source is False

    async def test_get_workspace_profile_returns_none(self):
        result = await get_workspace_profile("nonexistent@g.us")
        assert result is None

    async def test_duplicate_jid_or_folder_ownership_fails_closed(self):
        original = WorkspaceProfile(
            jid="discord:channel:one",
            name="Original",
            folder="one",
            trigger="@Pynchy",
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(original)

        with pytest.raises(ValueError, match="already owned by workspace"):
            await set_workspace_profile(replace(original, folder="two"))
        with pytest.raises(ValueError, match="use explicit rebind"):
            await set_workspace_profile(replace(original, jid="discord:channel:two"))

        assert await get_all_workspace_profiles() == {original.jid: original}

    async def test_explicit_workspace_rebind_atomically_replaces_jid(self):
        original = WorkspaceProfile(
            jid="discord:channel:old",
            name="Original",
            folder="one",
            trigger="@Pynchy",
            added_at="2024-01-01T00:00:00Z",
        )
        replacement = replace(
            original,
            jid="discord:channel:new",
            name="Replacement",
        )
        await set_workspace_profile(original)

        old_jid = await rebind_workspace_profile(replacement)

        assert old_jid == original.jid
        assert await get_workspace_profile(original.jid) is None
        assert await get_all_workspace_profiles() == {replacement.jid: replacement}

    async def test_explicit_rebind_rejects_invalid_profile(self):
        profile = WorkspaceProfile(
            jid="invalid-rebind@g.us",
            name="Valid",
            folder="invalid-rebind",
            trigger="@Valid",
        )

        with pytest.raises(ValueError, match="Invalid workspace profile"):
            await rebind_workspace_profile(replace(profile, name=""))

    async def test_explicit_rebind_rejects_a_jid_owned_by_another_workspace(self):
        first = WorkspaceProfile(
            jid="owned@g.us",
            name="First",
            folder="first",
            trigger="@First",
        )
        second = WorkspaceProfile(
            jid="other@g.us",
            name="Second",
            folder="second",
            trigger="@Second",
        )
        await set_workspace_profiles((first, second))

        with pytest.raises(ValueError, match="already owned by workspace"):
            await rebind_workspace_profile(replace(first, jid=second.jid))

    async def test_get_all_workspace_profiles(self):
        for i in range(2):
            profile = WorkspaceProfile(
                jid=f"ws-{i}@g.us",
                name=f"WS {i}",
                folder=f"ws-{i}",
                trigger=f"@WS{i}",
                added_at="2024-01-01T00:00:00Z",
            )
            await set_workspace_profile(profile)

        profiles = await get_all_workspace_profiles()
        assert len(profiles) == 2
        assert all(isinstance(p, WorkspaceProfile) for p in profiles.values())

    async def test_workspace_profile_validation_rejects_invalid(self):
        profile = WorkspaceProfile(
            jid="bad@g.us",
            name="",  # invalid: empty name
            folder="bad-ws",
            trigger="@Bad",
            added_at="2024-01-01T00:00:00Z",
        )
        with pytest.raises(ValueError, match="Workspace name is required"):
            await set_workspace_profile(profile)

    async def test_workspace_profile_admin_flag_roundtrip(self):
        profile = WorkspaceProfile(
            jid="admin-1@g.us",
            name="Admin",
            folder="admin-1",
            trigger="@Pynchy",
            is_admin=True,
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        result = await get_workspace_profile("admin-1@g.us")
        assert result is not None
        assert result.is_admin is True

    async def test_workspace_profile_batch_is_atomic(self):
        original = WorkspaceProfile(
            jid="one@g.us",
            name="One",
            folder="one",
            trigger="@Pynchy",
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(original)

        with pytest.raises(ValueError, match="already owned by workspace"):
            await set_workspace_profiles(
                (
                    replace(original, name="Changed"),
                    WorkspaceProfile(
                        jid="one@g.us",
                        name="Conflict",
                        folder="other",
                        trigger="@Pynchy",
                    ),
                )
            )

        assert await get_all_workspace_profiles() == {original.jid: original}

    async def test_workspace_profile_defaults_security_on_missing(self):
        """If security_profile column is NULL, defaults are used."""
        profile = WorkspaceProfile(
            jid="legacy@g.us",
            name="Legacy",
            folder="legacy",
            trigger="@Legacy",
            added_at="2024-01-01T00:00:00Z",
        )
        await set_workspace_profile(profile)

        # get_workspace_profile reads from the same table
        result = await get_workspace_profile("legacy@g.us")
        assert result is not None
        assert result.security.services == {}
        assert result.security.contains_secrets is False
        assert result.security.cop_active is True

    async def test_workspace_profile_defaults_security_on_legacy_null_column(self):
        profile = WorkspaceProfile(
            jid="legacy-null@g.us",
            name="Legacy",
            folder="legacy-null",
            trigger="@Legacy",
        )
        await set_workspace_profile(profile)
        async with atomic_write() as db:
            await db.execute(
                "UPDATE registered_groups SET security_profile = NULL WHERE jid = ?",
                (profile.jid,),
            )

        result = await get_workspace_profile(profile.jid)

        assert result is not None
        assert result.security.services == {}

    async def test_rebind_same_jid_does_not_delete_the_existing_row(self):
        profile = WorkspaceProfile(
            jid="same-jid@g.us",
            name="Same JID",
            folder="same-jid",
            trigger="@Same",
        )
        await set_workspace_profile(profile)

        assert await rebind_workspace_profile(profile) == profile.jid

    async def test_get_workspace_profile_raises_on_corrupt_security(self):
        """A corrupt security_profile must fail loud, not silently default trust."""
        async with atomic_write() as db:
            await db.execute(
                """INSERT OR REPLACE INTO registered_groups -- temporal-ok
                    (jid, name, folder, trigger_pattern, added_at,
                     container_config, security_profile, is_admin)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "corrupt@g.us",
                    "Corrupt",
                    "corrupt",
                    "@Corrupt",
                    "2024-01-01T00:00:00Z",
                    None,
                    "{not valid json",
                    0,
                ),
            )

        with pytest.raises(ValueError, match="Corrupt security_profile"):
            await get_workspace_profile("corrupt@g.us")


class TestChatHistoryLimit:
    async def test_respects_limit(self):
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        for i in range(10):
            await _store_message_row(
                _store(
                    message_id=f"msg-{i}",
                    chat_jid="group@g.us",
                    sender="123@s.whatsapp.net",
                    sender_name="Alice",
                    content=f"message {i}",
                    timestamp=f"2024-01-01T00:00:{i:02d}.000Z",
                )
            )

        messages = await get_chat_history("group@g.us", limit=3)
        assert len(messages) == 3
        # Newest last (reversed)
        assert messages[0].content == "message 7"
        assert messages[2].content == "message 9"

    async def test_returns_newest_last(self):
        """get_chat_history returns messages in chronological order (oldest first)."""
        await store_chat_metadata("group@g.us", "2024-01-01T00:00:00.000Z")
        await _store_message_row(
            _store(
                message_id="old",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="old",
                timestamp="2024-01-01T00:00:01.000Z",
            )
        )
        await _store_message_row(
            _store(
                message_id="new",
                chat_jid="group@g.us",
                sender="123@s.whatsapp.net",
                sender_name="Alice",
                content="new",
                timestamp="2024-01-01T00:00:02.000Z",
            )
        )

        messages = await get_chat_history("group@g.us", limit=50)
        assert messages[0].content == "old"
        assert messages[1].content == "new"


class TestGetTaskById:
    async def test_returns_none_for_nonexistent(self):
        result = await get_task_by_id("does-not-exist")
        assert result is None

    async def test_returns_full_task_fields(self):
        await create_task(_full_task())
        task = await get_task_by_id("full-task")
        assert task is not None
        _assert_full_task(task)


class TestGroupSync:
    async def test_get_returns_none_initially(self):
        result = await get_last_group_sync()
        assert result is None

    async def test_set_and_get_group_sync(self):
        await set_last_group_sync()
        result = await get_last_group_sync()
        assert result is not None
        # Should be a valid ISO timestamp
        assert "T" in result


class TestUpdateById:
    """Tests for the _update_by_id helper used by update_task and update_host_job."""

    async def test_update_task_updates_allowed_fields(self):
        """update_task should update fields in the allowlist."""
        await create_task(
            ScheduledTask(
                id="upd-1",
                group_folder="test",
                chat_jid="test@g.us",
                prompt="original",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await update_task("upd-1", {"status": "paused", "prompt": "updated"})
        task = await get_task_by_id("upd-1")
        assert task is not None
        assert task.status == "paused"
        assert task.prompt == "updated"

    async def test_update_task_ignores_disallowed_fields(self):
        """update_task should silently skip fields not in the allowlist."""
        await create_task(
            ScheduledTask(
                id="upd-2",
                group_folder="test",
                chat_jid="test@g.us",
                prompt="original",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        # Try to update group_folder which is not in the allowlist
        await update_task("upd-2", {"group_folder": "hacked", "status": "paused"})
        task = await get_task_by_id("upd-2")
        assert task is not None
        assert task.group_folder == "test"  # unchanged
        assert task.status == "paused"  # allowed field updated

    async def test_update_task_noop_with_no_allowed_fields(self):
        """update_task with only disallowed fields should be a safe no-op."""
        await create_task(
            ScheduledTask(
                id="upd-3",
                group_folder="test",
                chat_jid="test@g.us",
                prompt="original",
                schedule_type="once",
                schedule_value="2025-06-01T00:00:00.000Z",
                session_policy=SessionPolicy.RESET_BEFORE_RUN,
                next_run="2025-06-01T00:00:00.000Z",
                status="active",
                created_at="2024-01-01T00:00:00.000Z",
            )
        )

        await update_task("upd-3", {"id": "evil", "chat_jid": "evil@g.us"})
        task = await get_task_by_id("upd-3")
        assert task is not None
        assert task.status == "active"

    async def test_update_host_job_updates_allowed_fields(self):
        """update_host_job should update fields in the allowlist."""
        await create_host_job(
            {
                "id": "hj-upd-1",
                "name": "test-job",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "admin-1",
                "enabled": True,
            }
        )

        await update_host_job("hj-upd-1", {"status": "paused", "enabled": 0})
        job = await get_host_job_by_id("hj-upd-1")
        assert job is not None
        assert job.status == "paused"
        assert job.enabled is False

    async def test_update_host_job_ignores_disallowed_fields(self):
        """update_host_job should silently skip fields not in the allowlist."""
        await create_host_job(
            {
                "id": "hj-upd-2",
                "name": "test-job-2",
                "command": "echo hi",
                "schedule_type": "cron",
                "schedule_value": "0 9 * * *",
                "next_run": "2025-06-01T09:00:00Z",
                "status": "active",
                "created_at": "2024-01-01T00:00:00.000Z",
                "created_by": "admin-1",
                "enabled": True,
            }
        )

        # Try to update command which is not in the allowlist
        await update_host_job("hj-upd-2", {"command": "rm -rf /", "status": "paused"})
        job = await get_host_job_by_id("hj-upd-2")
        assert job is not None
        assert job.command == "echo hi"  # unchanged
        assert job.status == "paused"  # allowed field updated


async def test_late_work_item_transition_cannot_revive_cancelled_execution() -> None:
    issue = {
        "id": "issue-1",
        "identifier": "SYN-1",
        "url": "https://linear.app/example/issue/SYN-1",
        "state": {"id": "state-approved", "name": "Human Approved"},
    }
    execution = await create_work_item_claim(
        WorkItemClaimRequest(
            workspace="project",
            issue=issue,
            turn_id=None,
            task_id=None,
            initiated_by="test",
            request_id="claim-1",
        )
    )
    claim = await get_work_item_transition_by_request("claim-1")
    assert claim is not None
    execution = await resolve_work_item_transition(
        transition=claim,
        execution_status=WorkItemExecutionStatus.IN_PROGRESS,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )
    pending = await begin_work_item_transition(
        WorkItemTransitionRequest(
            execution=execution,
            request_id="complete-1",
            operation="complete",
            target_status="done",
            result_execution_status=WorkItemExecutionStatus.COMPLETED,
        )
    )
    await cancel_work_item_execution(execution.id, blocker="terminal callback")

    resolved = await resolve_work_item_transition(
        transition=pending,
        execution_status=WorkItemExecutionStatus.COMPLETED,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
        issue={**issue, "state": {"id": "state-done", "name": "Done"}},
    )

    assert resolved.status is WorkItemExecutionStatus.CANCELLED
    assert resolved.blocker == "terminal callback"


async def test_cancelling_unknown_work_item_execution_fails() -> None:
    with pytest.raises(ValueError, match="execution does not exist"):
        await cancel_work_item_execution("missing-execution", blocker="terminal callback")


async def test_execution_bindings_preserve_original_turn_and_task_owner() -> None:
    issue = {
        "id": "issue-2",
        "identifier": "SYN-2",
        "url": "https://linear.app/example/issue/SYN-2",
        "state": {"id": "state-approved", "name": "Human Approved"},
    }
    execution = await create_work_item_claim(
        WorkItemClaimRequest(
            workspace="project",
            issue=issue,
            turn_id=None,
            task_id="primary-task",
            initiated_by="test",
            request_id="claim-2",
        )
    )
    claim = await get_work_item_transition_by_request("claim-2")
    assert claim is not None
    execution = await resolve_work_item_transition(
        transition=claim,
        execution_status=WorkItemExecutionStatus.IN_PROGRESS,
        transition_status=WorkItemTransitionStatus.SUCCEEDED,
    )

    bound = await bind_work_item_execution_to_turn(
        execution.id,
        turn_id="turn-owner",
        task_id="primary-task",
    )
    assert bound.turn_id == "turn-owner"

    with pytest.raises(ValueError, match="another agent turn"):
        await bind_work_item_execution_to_turn(
            execution.id,
            turn_id="turn-other",
            task_id="primary-task",
        )
    with pytest.raises(ValueError, match="another durable task"):
        await bind_work_item_execution_to_task(
            execution.id,
            task_id="other-task",
            temporal_workflow_id="other-workflow",
        )
