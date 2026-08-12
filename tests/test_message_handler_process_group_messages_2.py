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
from unittest.mock import AsyncMock, call, patch

import pytest
from conftest import make_settings

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    ContainerOutput,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.conversation.models import (
    ConversationDeliveryStatus,
)
from pynchy.host.orchestrator.messaging.pipeline import (
    intercept_special_command,
    process_group_messages,
)
from pynchy.identifiers import (
    RuntimeId,
)
from pynchy.state import (
    begin_in_flight_turn,
    get_conversation_delivery,
    get_in_flight_turn,
    get_in_flight_turn_for_chat,
    init_test_database,
    prepare_in_flight_turn_recovery,
    store_message,
    upgrade_message_cursor,
)
from pynchy.turn_outcomes import TurnOutcome
from tests.message_handler_support import (
    _claimed_external_message,
    _make_deps,
    _make_group,
    _make_message,
    _patch_fmt_sdk,
    _patch_intercept,
    _patch_msgs_since,
    _run_loop_once,
)

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.pipeline.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"
_P_GET_RA = "pynchy.host.orchestrator.workspace_config.get_repo_access"

# Patch paths for names imported in _message_routing (routing/loop tests).
_PR = "pynchy.host.orchestrator.messaging.inbound"
_PR_NEW_MSGS = f"{_PR}.get_new_messages"
_PR_MSGS_SINCE = f"{_PR}.get_messages_since"
_PR_INTERCEPT = f"{_PR}.intercept_special_command"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestProcessGroupMessages:
    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_boundary_interrupt_preserves_the_follow_up_for_the_next_turn(self, tmp_path):
        """A safe host interruption commits only the current input cursor."""
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
        msg = _make_message(chat_jid=jid, timestamp="current-ts")
        completed: list[tuple[str, str]] = []

        async def complete_cursor(
            _deps,
            chat_jid,
            timestamp,
            _turn_id,
            *,
            conversation_claim_id=None,
        ):
            del conversation_claim_id
            await asyncio.sleep(0)
            completed.append((chat_jid, timestamp))

        deps.run_agent = AsyncMock(return_value="interrupted")
        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_fmt_sdk(),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.complete_turn_with_cursor",
                new=complete_cursor,
            ),
        ):
            result = await process_group_messages(deps, jid)

        assert result is TurnOutcome.CONTINUE_AFTER_SAFE_INTERRUPT
        assert completed == [(jid, "current-ts")]
        assert deps.pop_dispatched.call_args.args == (jid, "current-ts")

    @pytest.mark.asyncio
    async def test_non_admin_without_trigger_still_runs(self, tmp_path):
        """Workspace config no longer gates non-admin runs on mention triggers."""
        group = _make_group(is_admin=False)
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message("hello")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.COMPLETED
        deps.run_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_active_host_runner_defers_input_until_a_tool_result(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Host turns queue input because they cannot consume container IPC files."""
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False
        msg = _make_message(chat_jid=jid, timestamp="new-ts")
        monkeypatch.setattr("pynchy.config.access.get_settings", make_settings)

        with (
            patch(_PR_NEW_MSGS, new_callable=AsyncMock, return_value=([msg], "poll-ts")),
            patch(_PR_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(
            RuntimeId(group.folder)
        )
        deps.start_interactive_turn.assert_awaited_once_with(jid)

    @pytest.mark.asyncio
    async def test_cursor_rollback_on_save_state_failure(self, tmp_path):
        """Atomic turn completion failure rolls back the optimistic cursor."""
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={"g@g.us": group},
            last_agent_ts={"g@g.us": "old-ts"},
        )
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(
                "pynchy.host.orchestrator.messaging.cursor.complete_in_flight_turn",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB failure"),
            ),
            pytest.raises(RuntimeError, match="DB failure"),
        ):
            await process_group_messages(deps, "g@g.us")

        # Cursor rolls back so the DB (which still has "old-ts") stays consistent
        # with in-memory state. Messages will be re-processed on the next trigger.
        assert deps.last_agent_timestamp["g@g.us"] == "old-ts"

    @pytest.mark.asyncio
    async def test_trusted_external_route_preserves_provenance_without_public_taint(
        self,
        tmp_path,
    ):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
        external, _identity = await _claimed_external_message(
            jid,
            group,
            suffix="trusted-linear",
            provider="linear",
            public_source_input=False,
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[external]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        assert deps.run_agent.await_args.kwargs["input_source"] == "trusted:linear"

    @pytest.mark.asyncio
    async def test_agent_exception_preserves_routed_claim_for_durable_resume(self, tmp_path):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
        external, identity = await _claimed_external_message(
            jid,
            group,
            suffix="agent-exception",
        )
        deps.run_agent = AsyncMock(side_effect=RuntimeError("agent crashed"))

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(_P_MSGS_SINCE, new_callable=AsyncMock, side_effect=[[external], []]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            with pytest.raises(RuntimeError, match="agent crashed"):
                await process_group_messages(deps, jid)

            delivery = await get_conversation_delivery(identity)
            checkpoint = await get_in_flight_turn_for_chat(
                jid,
                {InFlightWorkKind.INTERACTIVE},
            )
            assert delivery is not None
            assert delivery.status is ConversationDeliveryStatus.CLAIMED
            assert checkpoint is not None
            assert checkpoint.conversation_claim_id == delivery.claim_id
            assert checkpoint.input_source == "external:matrix"

            deps.run_agent = AsyncMock(return_value="success")
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        completed = await get_conversation_delivery(identity)
        assert completed is not None
        assert completed.status is ConversationDeliveryStatus.COMPLETED
        assert deps.run_agent.await_args.kwargs["input_source"] == "external:matrix"
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], external.timestamp
        )

    @pytest.mark.asyncio
    async def test_finalization_exception_preserves_routed_claim_for_resume(self, tmp_path):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
        external, identity = await _claimed_external_message(
            jid,
            group,
            suffix="finalization-exception",
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(_P_MSGS_SINCE, new_callable=AsyncMock, side_effect=[[external], []]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.complete_turn_with_cursor",
                new_callable=AsyncMock,
                side_effect=RuntimeError("cursor commit failed"),
            ),
        ):
            with pytest.raises(RuntimeError, match="cursor commit failed"):
                await process_group_messages(deps, jid)

            delivery = await get_conversation_delivery(identity)
            checkpoint = await get_in_flight_turn_for_chat(
                jid,
                {InFlightWorkKind.INTERACTIVE},
            )
            assert delivery is not None
            assert delivery.status is ConversationDeliveryStatus.CLAIMED
            assert checkpoint is not None
            assert checkpoint.conversation_claim_id == delivery.claim_id

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        completed = await get_conversation_delivery(identity)
        assert completed is not None
        assert completed.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], external.timestamp
        )

    @pytest.mark.asyncio
    async def test_restart_semantically_resumes_partial_turn_without_replaying_input(
        self, tmp_path
    ):
        """A killed turn continues in its existing thread and clears its checkpoint."""
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})
        msg = _make_message("do the whole job", timestamp="new-ts")

        async def interrupted_run(_group, _jid, _messages, on_output=None, *_args, **_kwargs):
            assert on_output is not None
            await on_output(ContainerOutput(status="success", result="partial result"))
            raise asyncio.CancelledError

        deps.run_agent = AsyncMock(side_effect=interrupted_run)
        deps.handle_streamed_output = AsyncMock(return_value=True)
        recovered_messages: list[dict] = []

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                side_effect=[[msg], []],
            ),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            with pytest.raises(asyncio.CancelledError):
                await process_group_messages(deps, jid)

            checkpoint = await get_in_flight_turn_for_chat(
                jid,
                {InFlightWorkKind.INTERACTIVE},
            )
            assert checkpoint is not None
            assert checkpoint.output_sent is True
            original_turn_id = checkpoint.turn_id
            await prepare_in_flight_turn_recovery("deploy-sha")

            def resumed_run(
                _group,
                _jid,
                messages,
                _on_output=None,
                *_args,
                **_kwargs,
            ):
                recovered_messages.extend(messages)
                return "success"

            deps.run_agent = AsyncMock(side_effect=resumed_run)
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        assert len(recovered_messages) == 1
        recovery_prompt = recovered_messages[0]
        assert recovery_prompt["metadata"]["interrupted_turn_id"] == original_turn_id
        assert recovery_prompt["content"]
        original_input = checkpoint.input_messages[0]["content"]
        assert isinstance(original_input, str)
        assert recovery_prompt["content"].endswith(f"User: {original_input}")
        assert (
            await get_in_flight_turn_for_chat(
                jid,
                {InFlightWorkKind.INTERACTIVE},
            )
            is None
        )
        assert deps.last_agent_timestamp[jid] == "new-ts"

    @pytest.mark.asyncio
    async def test_recovery_uses_runtime_current_chat_binding(self, tmp_path):
        """A replacement JID changes delivery, not the checkpoint's stable owner."""
        old_jid = "slack:old-thread"
        current_jid = "slack:current-thread"
        group = _make_group(folder="stable-runtime", is_admin=True)
        deps = _make_deps(
            groups={current_jid: group},
            last_agent_ts={current_jid: "current-cursor"},
        )
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-before-rebind",
                chat_jid=old_jid,
                group_folder=group.folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"sender_name": "Alice", "content": "Finish the job."}],
                input_start_cursor="old-start",
                input_end_cursor="old-end",
                started_at="2026-07-25T10:00:00+00:00",
                claimed_at=None,
            )
        )

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([]),
        ):
            assert await process_group_messages(deps, current_jid) is TurnOutcome.COMPLETED

        run = deps.run_agent.await_args
        assert run.args[1] == current_jid
        assert deps.set_typing_on_channels.await_args_list == [
            call(current_jid, is_typing=True),
            call(current_jid, is_typing=False),
        ]
        assert await get_in_flight_turn("turn-before-rebind") is None
        assert deps.last_agent_timestamp[current_jid] == "current-cursor"

    @pytest.mark.asyncio
    async def test_pause_stops_active_turn_without_retry_or_error_warning(self, tmp_path):
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "2026-07-25T09:59:00+00:00"},
        )
        deps.queue.has_active_run.return_value = True
        original = _make_message(
            "do the whole job",
            message_id="original",
            chat_jid=jid,
            timestamp="2026-07-25T10:00:00+00:00",
        )
        await store_message(original)
        agent_entered = asyncio.Event()
        stop_agent = asyncio.Event()

        async def interrupted_run(*_args, **_kwargs):
            agent_entered.set()
            await stop_agent.wait()
            return "error"

        def stop_for_control(_jid):
            stop_agent.set()

        deps.run_agent.side_effect = interrupted_run
        deps.queue.stop_active_process_for_control.side_effect = stop_for_control

        with patch.object(deps, "message_data_dir", tmp_path):
            processing = asyncio.create_task(process_group_messages(deps, jid))
            await asyncio.wait_for(agent_entered.wait(), timeout=1.0)
            pause = _make_message(
                "pause",
                message_id="pause-active",
                chat_jid=jid,
                timestamp="2026-07-25T10:00:01+00:00",
            )
            await store_message(pause)
            assert await intercept_special_command(deps, jid, group, pause) is True
            assert await processing is TurnOutcome.PAUSED

        checkpoint = await get_in_flight_turn_for_chat(
            jid,
            {InFlightWorkKind.INTERACTIVE},
        )
        assert checkpoint is not None
        assert checkpoint.control_state is CheckpointControlState.PAUSED
        assert checkpoint.claimed_at is None
        assert checkpoint.input_end_cursor == await upgrade_message_cursor(
            [jid], original.timestamp
        )
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], pause.timestamp
        )
        assert deps.broadcast_host_message.await_args_list == [
            call(jid, "⏸️"),
        ]

    @pytest.mark.asyncio
    async def test_next_message_resumes_paused_turn_in_same_provider_session(self, tmp_path):
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        pause_timestamp = "2026-07-25T10:00:01+00:00"
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: pause_timestamp})
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-paused-resume",
                chat_jid=jid,
                group_folder=group.folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[
                    {
                        "message_type": "user",
                        "sender": "alice",
                        "sender_name": "Alice",
                        "content": "Write and publish the draft.",
                        "timestamp": "2026-07-25T10:00:00+00:00",
                        "metadata": None,
                    }
                ],
                input_start_cursor="old-ts",
                input_end_cursor="2026-07-25T10:00:00+00:00",
                started_at="2026-07-25T10:00:00+00:00",
                session_id="provider-thread-123",
                conversation_claim_id=None,
                input_source="trusted:linear",
                control_state=CheckpointControlState.PAUSED,
            )
        )
        guidance = _make_message(
            "Continue, but leave it unpublished.",
            message_id="resume-guidance",
            chat_jid=jid,
            timestamp="2026-07-25T10:00:02+00:00",
        )
        await store_message(guidance)

        with patch.object(deps, "message_data_dir", tmp_path):
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        deps.run_agent.assert_awaited_once()
        run = deps.run_agent.await_args
        assert run.kwargs["turn_id"] == "turn-paused-resume"
        assert run.kwargs["resume_session_id"] == "provider-thread-123"
        assert run.kwargs["input_source"] == "trusted:linear"
        resumed_messages = run.args[2]
        assert resumed_messages[0]["metadata"]["source"] == "pause_continuation"
        assert resumed_messages[1]["content"] == guidance.content
        assert resumed_messages[1]["metadata"]["checkpoint_guidance"] is True
        assert await get_in_flight_turn("turn-paused-resume") is None
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor(
            [jid], guidance.timestamp
        )

    @pytest.mark.asyncio
    async def test_reply_reactivates_frozen_scheduled_occurrence(self, tmp_path):
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        pause_timestamp = "2026-07-25T10:00:01+00:00"
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: pause_timestamp})
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="scheduled-paused",
                chat_jid=jid,
                group_folder=group.folder,
                work_kind=InFlightWorkKind.SCHEDULED,
                input_messages=[
                    {
                        "message_type": "user",
                        "sender": "system",
                        "sender_name": "System",
                        "content": "Run the weekly report.",
                        "timestamp": "2026-07-25T10:00:00+00:00",
                        "metadata": {"source": "scheduled_task"},
                    }
                ],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-25T10:00:00+00:00",
                task_id="weekly-report",
                session_id="scheduled-provider-thread",
                input_source="scheduled_task",
                control_state=CheckpointControlState.PAUSED,
            )
        )
        guidance = _make_message(
            "Resume and omit the finance section.",
            message_id="scheduled-guidance",
            chat_jid=jid,
            timestamp="2026-07-25T10:00:02+00:00",
        )
        await store_message(guidance)

        with patch.object(deps, "message_data_dir", tmp_path):
            assert await process_group_messages(deps, jid) is TurnOutcome.COMPLETED

        deps.run_agent.assert_not_awaited()
        deps.start_interrupted_turn.assert_awaited_once_with(
            "scheduled-paused",
            group.folder,
        )
        checkpoint = await get_in_flight_turn("scheduled-paused")
        assert checkpoint is not None
        assert checkpoint.control_state is CheckpointControlState.ACTIVE
        assert checkpoint.claimed_at is None
        assert checkpoint.session_id == "scheduled-provider-thread"
        assert checkpoint.input_end_cursor == await upgrade_message_cursor(
            [jid], guidance.timestamp
        )
        assert checkpoint.input_messages[-1]["content"] == guidance.content

    @pytest.mark.asyncio
    async def test_agent_error_rolls_back_cursor(self, tmp_path):
        """Agent error with no output → cursor unchanged (never advanced), user notified."""
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={"g@g.us": group},
            last_agent_ts={"g@g.us": "old-ts"},
        )
        deps.run_agent = AsyncMock(return_value="error")
        deps.handle_streamed_output = AsyncMock(return_value=False)
        deps.save_state = AsyncMock()
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch.object(deps, "message_data_dir", tmp_path),
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            result = await process_group_messages(deps, "g@g.us")

        assert result is TurnOutcome.RETRY
        assert deps.last_agent_timestamp["g@g.us"] == "old-ts"
        deps.broadcast_host_message.assert_awaited_once()
        host_text = deps.broadcast_host_message.call_args[0][1]
        assert "error" in host_text.lower()
