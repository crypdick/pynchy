"""Tests for message processing (message_handler) and routing (_message_routing).

Covers:
- intercept_special_command: reset, end session, redeploy, !commands
- process_group_messages: reset handoff, trigger filtering, cursor management,
  dirty repo check, error rollback, worktree merge
- _check_dirty_repo, advance_cursor, and reset handoff behavior
- start_message_loop: "btw" non-interrupting messages during active tasks
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    ContainerOutput,
)
from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationDeliveryStatus,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.host.orchestrator.messaging.cursor import (
    complete_turn_with_cursor as persist_completed_turn,
)
from pynchy.host.orchestrator.messaging.pipeline import (
    process_group_messages,
)
from pynchy.host.orchestrator.messaging.turn_recovery import (
    resume_interrupted_message_if_present,
)
from pynchy.identifiers import (
    GroupFolder,
    RuntimeId,
)
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    claim_next_conversation_delivery,
    get_chat_history,
    get_conversation_delivery,
    init_test_database,
    store_message,
    upgrade_message_cursor,
)
from pynchy.state import get_messages_since as get_stored_messages_since
from pynchy.turn_outcomes import TurnOutcome
from tests.message_handler_support import (
    _claimed_external_message,
    _make_deps,
    _make_group,
    _make_message,
    _patch_fmt_sdk,
    _patch_msgs_since,
    _run_loop_once,
)

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.sender_policy.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"

# Patch paths for names imported in _message_routing (routing/loop tests).
_PR = "pynchy.host.orchestrator.messaging.inbound"
_PR_NEW_MSGS = f"{_PR}.get_new_messages"
_PR_MSGS_SINCE = "pynchy.host.orchestrator.messaging.sender_policy.get_messages_since"
_PR_INTERCEPT = f"{_PR}.intercept_special_command"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestProcessGroupMessages:
    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_returns_true_for_unknown_group(self):
        """Unknown group JID should complete without work."""
        deps = _make_deps(groups={})
        result = await process_group_messages(deps, "unknown@g.us")
        assert result is TurnOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_reset_handoff_file_processed(self, tmp_path):
        """reset_prompt.json consumed → agent invoked."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        deps.handle_streamed_output.return_value = True

        ipc_dir = tmp_path / "ipc" / "test-group"
        ipc_dir.mkdir(parents=True)
        reset_file = ipc_dir / "reset_prompt.json"
        reset_file.write_text(json.dumps({"message": "Hello after reset"}))

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_awaited_once()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_reset_handoff_propagates_cancellation(self, tmp_path):
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        deps.run_agent.side_effect = asyncio.CancelledError()
        reset_file = tmp_path / "ipc" / "test-group" / "reset_prompt.json"
        reset_file.parent.mkdir(parents=True)
        reset_file.write_text(json.dumps({"message": "Hello"}))

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
            pytest.raises(asyncio.CancelledError),
        ):
            await process_group_messages(deps, "g@g.us")

    @pytest.mark.asyncio
    async def test_reset_handoff_releases_claim_when_agent_fails(self, tmp_path):
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        deps.run_agent.side_effect = RuntimeError("agent failed")
        reset_file = tmp_path / "ipc" / "test-group" / "reset_prompt.json"
        reset_file.parent.mkdir(parents=True)
        reset_file.write_text(json.dumps({"message": "Hello"}))

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
            pytest.raises(RuntimeError, match="agent failed"),
        ):
            await process_group_messages(deps, "g@g.us")

    @pytest.mark.asyncio
    async def test_reset_handoff_with_dirty_repo_check(self, tmp_path):
        """needsDirtyRepoCheck flag creates the dirty check file."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})

        ipc_dir = tmp_path / "ipc" / "test-group"
        ipc_dir.mkdir(parents=True)
        reset_file = ipc_dir / "reset_prompt.json"
        reset_file.write_text(
            json.dumps(
                {
                    "message": "Hello",
                    "needsDirtyRepoCheck": True,
                }
            )
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        assert (ipc_dir / "needs_dirty_check.json").exists()

    @pytest.mark.asyncio
    async def test_interrupted_turn_already_claimed_is_complete(self):
        deps = _make_deps()
        turn = MagicMock(
            control_state=CheckpointControlState.ACTIVE,
            turn_id="turn-already-claimed",
        )

        with (
            patch(
                "pynchy.host.orchestrator.messaging.turn_recovery.get_oldest_resumable_turn_for_group",
                new_callable=AsyncMock,
                return_value=turn,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.turn_recovery.claim_in_flight_turn",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await resume_interrupted_message_if_present(
                deps,
                "g@g.us",
                _make_group(),
                AsyncMock(),
            )

        assert result is TurnOutcome.COMPLETED

    @pytest.mark.asyncio
    async def test_reset_handoff_malformed_json_falls_through(self, tmp_path):
        """Malformed reset_prompt.json → clean up and fall through to normal processing."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})

        ipc_dir = tmp_path / "ipc" / "test-group"
        ipc_dir.mkdir(parents=True)
        reset_file = ipc_dir / "reset_prompt.json"
        reset_file.write_text("NOT VALID JSON")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_not_awaited()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_no_messages_returns_true(self, tmp_path):
        """No pending messages completes without starting an agent."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inline_approval_is_consumed_while_prior_external_claim_runs(
        self,
        tmp_path,
    ):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        identity = ExternalDeliveryIdentity(
            provider=ExternalProvider("matrix"),
            route=ExternalRoute("personal:family"),
            delivery_id=ExternalDeliveryId("$mixed-event"),
        )
        await admit_external_delivery_receipt(
            ExternalDeliveryReceipt(
                identity=identity,
                payload_sha256="sha",
                received_at="2026-07-19T12:00:00+00:00",
            )
        )
        admission = await admit_conversation_delivery(
            identity,
            ConversationSubject(
                namespace=ConversationSubjectNamespace("matrix:me:family:room"),
                key=ConversationSubjectKey("!family:example.com"),
            ),
            GroupFolder(group.folder),
        )
        claim_id = ConversationClaimId("claim-mixed")
        assert await claim_next_conversation_delivery(admission.conversation.id, claim_id)
        external = _make_message(
            "provider says approve ab",
            message_id="$mixed-event",
            chat_jid=jid,
            sender="@stranger:matrix.example.com",
            timestamp="2026-07-19T12:00:01+00:00",
            metadata={
                "authenticated_external_route": True,
                "external_provider": "matrix",
                "conversation_id": admission.conversation.id,
                "conversation_claim_id": claim_id,
            },
        )
        approval = _make_message(
            "approve ab",
            message_id="discord-approval",
            chat_jid=jid,
            sender="discord:123456",
            timestamp="2026-07-19T12:00:02+00:00",
        )
        await store_message(external)
        await store_message(approval)

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
            ) as handle_approval,
        ):
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        handle_approval.assert_awaited_once_with(
            deps,
            jid,
            "approve",
            "ab",
            "discord:123456",
        )
        agent_messages = deps.run_agent.await_args.args[2]
        assert [message["content"] for message in agent_messages] == [external.content]
        delivery = await get_conversation_delivery(identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        history = await get_chat_history(jid)
        consumed = next(message for message in history if message.id == approval.id)
        assert consumed.message_type == "host"
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], approval.timestamp
        )

    @pytest.mark.asyncio
    async def test_active_inline_approval_completes_after_external_claim_succeeds(
        self,
        tmp_path,
    ):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        external, identity = await _claimed_external_message(
            jid,
            group,
            suffix="active-success",
        )
        deps.last_timestamp = external.timestamp
        agent_entered = asyncio.Event()
        release_agent = asyncio.Event()

        async def run_agent(*args, **_kwargs):
            agent_entered.set()
            await release_agent.wait()
            await args[3](ContainerOutput(status="success", result="done"))
            return "success"

        deps.run_agent.side_effect = run_agent
        approval = _make_message(
            "approve ab",
            message_id="approval-active-success",
            chat_jid=jid,
            sender="discord:operator",
            timestamp="2026-07-19T12:00:02+00:00",
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
            ) as handle_approval,
        ):
            processing = asyncio.create_task(process_group_messages(deps, jid))
            await asyncio.wait_for(agent_entered.wait(), timeout=1.0)
            await store_message(approval)
            await _run_loop_once(deps)

            handle_approval.assert_awaited_once()
            assert not deps.last_agent_timestamp.get(jid, "")
            release_agent.set()
            assert await processing is TurnOutcome.COMPLETED

        delivery = await get_conversation_delivery(identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], approval.timestamp
        )

    @pytest.mark.asyncio
    async def test_active_inline_approval_survives_clean_external_retry(
        self,
        tmp_path,
    ):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        external, identity = await _claimed_external_message(
            jid,
            group,
            suffix="active-retry",
        )
        deps.last_timestamp = external.timestamp
        agent_entered = asyncio.Event()
        release_agent = asyncio.Event()

        async def fail_agent(*_args, **_kwargs):
            agent_entered.set()
            await release_agent.wait()
            return "error"

        deps.run_agent.side_effect = fail_agent
        approval = _make_message(
            "approve ab",
            message_id="approval-active-retry",
            chat_jid=jid,
            sender="discord:operator",
            timestamp="2026-07-19T12:00:02+00:00",
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
            ) as handle_approval,
        ):
            processing = asyncio.create_task(process_group_messages(deps, jid))
            await asyncio.wait_for(agent_entered.wait(), timeout=1.0)
            await store_message(approval)
            await _run_loop_once(deps)
            release_agent.set()
            assert await processing is TurnOutcome.RETRY

            delivery = await get_conversation_delivery(identity)
            assert delivery is not None
            assert delivery.status is ConversationDeliveryStatus.CLAIMED
            assert delivery.claim_id == external.metadata["conversation_claim_id"]
            assert not deps.last_agent_timestamp.get(jid, "")

            async def completed_agent(*args, **_kwargs):
                await args[3](ContainerOutput(status="success", result="done"))
                return "success"

            deps.run_agent = AsyncMock(side_effect=completed_agent)
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        handle_approval.assert_awaited_once()
        retried_delivery = await get_conversation_delivery(identity)
        assert retried_delivery is not None
        assert retried_delivery.status is ConversationDeliveryStatus.COMPLETED
        retried_messages = deps.run_agent.await_args.args[2]
        assert [message["content"] for message in retried_messages] == [external.content]
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], approval.timestamp
        )

    @pytest.mark.asyncio
    async def test_active_approval_is_removed_before_follow_up_is_forwarded(
        self,
        tmp_path,
    ):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        external, identity = await _claimed_external_message(
            jid,
            group,
            suffix="active-follow-up",
        )
        deps.last_timestamp = external.timestamp
        deps.queue.send_message.return_value = True
        agent_entered = asyncio.Event()
        release_agent = asyncio.Event()

        async def run_agent(*args, **_kwargs):
            agent_entered.set()
            await release_agent.wait()
            await args[3](ContainerOutput(status="success", result="done"))
            return "success"

        deps.run_agent.side_effect = run_agent
        approval = _make_message(
            "approve ab",
            message_id="approval-before-follow-up",
            chat_jid=jid,
            sender="discord:operator",
            timestamp="2026-07-19T12:00:02+00:00",
        )
        follow_up = _make_message(
            "also tell them tomorrow works",
            message_id="ordinary-follow-up",
            chat_jid=jid,
            sender="discord:operator",
            timestamp="2026-07-19T12:00:03+00:00",
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
            ) as handle_approval,
        ):
            processing = asyncio.create_task(process_group_messages(deps, jid))
            await asyncio.wait_for(agent_entered.wait(), timeout=1.0)
            await store_message(approval)
            await store_message(follow_up)
            await _run_loop_once(deps)
            release_agent.set()
            assert await processing is TurnOutcome.COMPLETED

        handle_approval.assert_awaited_once()
        deps.queue.send_message.assert_called_once_with(
            RuntimeId(group.folder),
            f"{follow_up.sender_name}: {follow_up.content}",
        )
        assert "approve" not in deps.queue.send_message.call_args.args[1]
        delivery = await get_conversation_delivery(identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], follow_up.timestamp
        )

    @pytest.mark.asyncio
    async def test_late_approval_waits_for_turn_finalization_without_duplicate_delivery(
        self,
        tmp_path,
    ):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        external, identity = await _claimed_external_message(
            jid,
            group,
            suffix="late-finalization",
        )
        deps.last_timestamp = external.timestamp
        finalization_entered = asyncio.Event()
        release_finalization = asyncio.Event()
        pending_loaded = asyncio.Event()

        async def blocked_complete(*args, **kwargs):
            finalization_entered.set()
            await release_finalization.wait()
            await persist_completed_turn(*args, **kwargs)

        async def tracked_messages_since(*args, **kwargs):
            messages = await get_stored_messages_since(*args, **kwargs)
            pending_loaded.set()
            return messages

        approval = _make_message(
            "approve ab",
            message_id="approval-late-finalization",
            chat_jid=jid,
            sender="discord:operator",
            timestamp="2026-07-19T12:00:02+00:00",
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.complete_turn_with_cursor",
                new=blocked_complete,
            ),
            patch(_PR_MSGS_SINCE, new=tracked_messages_since),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
            ) as handle_approval,
        ):
            processing = asyncio.create_task(process_group_messages(deps, jid))
            await asyncio.wait_for(finalization_entered.wait(), timeout=1.0)
            await store_message(approval)
            routing = asyncio.create_task(_run_loop_once(deps))
            await asyncio.wait_for(pending_loaded.wait(), timeout=1.0)
            release_finalization.set()
            assert await processing is TurnOutcome.COMPLETED
            await routing

        handle_approval.assert_awaited_once()
        deps.start_interactive_turn.assert_not_awaited()
        delivery = await get_conversation_delivery(identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], approval.timestamp
        )

    @pytest.mark.asyncio
    async def test_polling_and_recovery_queue_cannot_execute_same_approval_twice(
        self,
        tmp_path,
    ):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        approval = _make_message(
            "approve ab",
            message_id="approval-concurrent-classifiers",
            chat_jid=jid,
            sender="discord:operator",
            timestamp="2026-07-19T12:00:02+00:00",
        )
        await store_message(approval)
        handler_entered = asyncio.Event()
        release_handler = asyncio.Event()

        async def blocked_handler(*_args, **_kwargs):
            handler_entered.set()
            await release_handler.wait()

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
                side_effect=blocked_handler,
            ) as handle_approval,
        ):
            recovery_queue = asyncio.create_task(process_group_messages(deps, jid))
            await asyncio.wait_for(handler_entered.wait(), timeout=1.0)
            polling = asyncio.create_task(_run_loop_once(deps))
            await asyncio.sleep(0)
            release_handler.set()
            assert await recovery_queue is TurnOutcome.COMPLETED
            await polling

        handle_approval.assert_awaited_once()
        history = await get_chat_history(jid)
        consumed = next(message for message in history if message.id == approval.id)
        assert consumed.message_type == "host"

    @pytest.mark.asyncio
    async def test_tool_result_delivers_a_deferred_interrupt(self, tmp_path):
        """A completed tool is the safe boundary for queued user input."""
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        msg = _make_message()
        deps.queue.interrupt_after_tool_result = AsyncMock(return_value=True)

        async def run_agent(*args, **kwargs):
            on_output = args[3]
            await on_output(
                ContainerOutput(
                    status="success",
                    type="tool_result",
                    tool_result_id="tool-1",
                    tool_result_content="done",
                )
            )
            await on_output(ContainerOutput(status="success", result="done"))
            return "success"

        deps.run_agent.side_effect = run_agent

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_fmt_sdk(),
        ):
            result = await process_group_messages(deps, jid)

        assert result is TurnOutcome.COMPLETED
        deps.queue.interrupt_after_tool_result.assert_awaited_once_with(RuntimeId(group.folder))
