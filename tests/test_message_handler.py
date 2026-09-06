"""Tests for message processing (message_handler) and routing (_message_routing).

Covers:
- intercept_special_command: reset, end session, redeploy, !commands
- process_group_messages: reset handoff, trigger filtering, cursor management,
  dirty repo check, error rollback, worktree merge
- _check_dirty_repo, advance_cursor, and reset handoff behavior
- start_message_loop: "btw" non-interrupting messages during active tasks
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.agent_protocol.api import (
    CheckpointControlState,
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.host.orchestrator import session_handler
from pynchy.host.orchestrator.host_shell import ShellResult
from pynchy.host.orchestrator.messaging.host_controls import (
    intercept_immediate_checkpoint_controls,
    reclassify_host_control,
)
from pynchy.host.orchestrator.messaging.pipeline import (
    execute_direct_command,
    intercept_special_command,
)
from pynchy.identifiers import (
    RuntimeId,
)
from pynchy.state import (
    begin_in_flight_turn,
    get_chat_history,
    get_in_flight_turn,
    init_test_database,
    store_message,
    upgrade_message_cursor,
)
from tests.message_handler_support import (
    _make_deps,
    _make_group,
    _make_message,
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


class TestInterceptSpecialCommand:
    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.parametrize("content", ["approve ab", "deny ab", "redeploy", "!whoami"])
    @pytest.mark.asyncio
    async def test_external_route_text_is_never_a_control_command(self, content: str):
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message(
            content,
            metadata={
                "authenticated_external_route": True,
                "conversation_claim_id": "claim-1",
            },
        )

        assert await intercept_special_command(deps, "g@g.us", group, msg) is False
        deps.handle_context_reset.assert_not_awaited()
        deps.handle_end_session.assert_not_awaited()
        deps.trigger_manual_redeploy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approval_records_stable_sender_identity(self):
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message(
            "approve ab",
            sender="discord:123456",
            sender_name="Mutable Display Name",
        )
        with (
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_redeploy",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_approval_command",
                return_value=("approve", "ab"),
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
            ) as handle,
        ):
            assert await intercept_special_command(deps, "g@g.us", group, msg) is True

        handle.assert_awaited_once_with(deps, "g@g.us", "approve", "ab", "discord:123456")

    @pytest.mark.asyncio
    async def test_context_reset_intercepted(self):
        """Reset patterns should trigger handle_context_reset."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message("reset context")

        with patch(
            "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
            return_value=True,
        ):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is True
        deps.handle_context_reset.assert_awaited_once_with(
            "g@g.us", group, msg.timestamp, source_message=msg
        )

    @pytest.mark.asyncio
    async def test_pause_consumes_command_and_requests_checkpoint_stop(self):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        channel = MagicMock()
        channel.owns_jid.return_value = True
        deps.channels = [channel]
        deps.queue.has_active_run.return_value = True
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-pause",
                chat_jid=jid,
                group_folder=group.folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[{"sender_name": "Alice", "content": "finish this"}],
                input_start_cursor="old-ts",
                input_end_cursor="input-ts",
                started_at="2026-07-25T10:00:00+00:00",
                session_id="provider-thread",
                claimed_at="2026-07-25T10:00:01+00:00",
            )
        )
        msg = _make_message(
            "pause",
            message_id="pause-command",
            chat_jid=jid,
            timestamp="pause-ts",
        )
        await store_message(msg)

        assert await intercept_special_command(deps, jid, group, msg) is True

        checkpoint = await get_in_flight_turn("turn-pause")
        assert checkpoint is not None
        assert checkpoint.control_state is CheckpointControlState.PAUSE_REQUESTED
        assert checkpoint.session_id == "provider-thread"
        history = await get_chat_history(jid)
        assert next(message for message in history if message.id == msg.id).message_type == "host"
        assert deps.last_agent_timestamp[jid] == await upgrade_message_cursor([jid], msg.timestamp)
        deps.queue.stop_active_process_for_control.assert_awaited_once_with(RuntimeId(group.folder))
        deps.queue.destroy_runtime_session.assert_awaited_once_with(RuntimeId(group.folder))
        deps.send_reaction_to_channels.assert_awaited_once_with(jid, msg.id, msg.sender, "⏸️")

    @pytest.mark.asyncio
    async def test_context_reset_clears_an_idle_checkpoint(self):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-reset",
                chat_jid=jid,
                group_folder=group.folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-25T10:00:00+00:00",
            )
        )
        msg = _make_message("reset context", chat_jid=jid, message_id="reset-command")
        await store_message(msg)

        with patch(
            "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
            return_value=True,
        ):
            assert await intercept_special_command(deps, jid, group, msg) is True

        assert await get_in_flight_turn("turn-reset") is None

    @pytest.mark.asyncio
    async def test_repeated_pause_is_idempotent(self):
        jid = "g@g.us"
        group = _make_group()
        deps = _make_deps(groups={jid: group})
        await begin_in_flight_turn(
            InFlightTurn(
                turn_id="turn-paused",
                chat_jid=jid,
                group_folder=group.folder,
                work_kind=InFlightWorkKind.INTERACTIVE,
                input_messages=[],
                input_start_cursor="",
                input_end_cursor="",
                started_at="2026-07-25T10:00:00+00:00",
                control_state=CheckpointControlState.PAUSED,
            )
        )
        commands = [
            _make_message(
                "stop",
                message_id="stop-one",
                chat_jid=jid,
                timestamp="2026-07-25T10:01:00+00:00",
            ),
            _make_message(
                "pause",
                message_id="pause-two",
                chat_jid=jid,
                timestamp="2026-07-25T10:02:00+00:00",
            ),
        ]
        for command in commands:
            await store_message(command)

        for command in commands:
            assert await intercept_special_command(deps, jid, group, command) is True

        checkpoint = await get_in_flight_turn("turn-paused")
        assert checkpoint is not None
        assert checkpoint.control_state is CheckpointControlState.PAUSED
        assert checkpoint.claimed_at is None
        assert deps.broadcast_host_message.await_count == 2

    @pytest.mark.asyncio
    async def test_end_session_intercepted(self):
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message("end session")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_end_session",
                return_value=True,
            ),
        ):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is True
        deps.handle_end_session.assert_awaited_once_with(
            "g@g.us", group, msg.timestamp, source_message=msg
        )

    @pytest.mark.asyncio
    async def test_redeploy_intercepted(self):
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message("redeploy")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_redeploy",
                return_value=True,
            ),
        ):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is True
        deps.trigger_manual_redeploy.assert_awaited_once_with("g@g.us", source_message=msg)
        assert deps.last_agent_timestamp["g@g.us"] == msg.timestamp
        deps.save_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_manual_redeploy_reacts_to_the_source_message(self):
        msg = _make_message("redeploy", chat_jid="g@g.us")
        deps = MagicMock(spec=session_handler.SessionDeps)
        deps.current_deploy_revision.return_value = ("a" * 40, "config-hash")

        with (
            patch(
                "pynchy.host.orchestrator.session_handler._send_command_confirmation",
                new_callable=AsyncMock,
            ) as confirmation,
            patch(
                "pynchy.host.orchestrator.session_handler.start_deploy_workflow",
                new_callable=AsyncMock,
            ) as start_deploy,
        ):
            await session_handler.trigger_manual_redeploy(deps, "g@g.us", source_message=msg)

        confirmation.assert_awaited_once_with(deps, "g@g.us", msg, "🔄")
        start_deploy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bang_command_intercepted(self):
        """!commands should be executed directly without LLM."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message("!ls -la")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_redeploy",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.host_controls.execute_direct_command",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is True
        mock_exec.assert_awaited_once_with(deps, "g@g.us", group, msg, "ls -la")

    @pytest.mark.asyncio
    async def test_bang_alone_not_intercepted(self):
        """A lone '!' with no command should not be intercepted."""
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_redeploy",
                return_value=False,
            ),
        ):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is False

    @pytest.mark.asyncio
    async def test_pending_query_is_intercepted(self):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("pending")

        with patch(
            "pynchy.host.orchestrator.messaging.host_controls.approval_handler."
            "handle_pending_query",
            new_callable=AsyncMock,
        ) as handle:
            assert await intercept_special_command(deps, "g@g.us", group, msg) is True

        handle.assert_awaited_once_with(deps, "g@g.us")

    @pytest.mark.asyncio
    async def test_immediate_controls_skip_host_messages_and_enqueue_remaining_input(self):
        group = _make_group()
        deps = _make_deps()
        host = _make_message("pause", message_id="already-host")
        host.message_type = "host"
        pending = _make_message("pause", message_id="pause-command")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.sender_policy.get_messages_since",
                new_callable=AsyncMock,
                return_value=[host, pending],
            ),
            patch(
                "pynchy.host.orchestrator.messaging.host_controls.intercept_special_command",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await intercept_immediate_checkpoint_controls(deps, "g@g.us", group, [pending])

        assert result is True
        deps.start_interactive_turn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_immediate_controls_return_none_when_no_command_is_handled(self):
        group = _make_group()
        deps = _make_deps()
        pending = _make_message("pause")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.sender_policy.get_messages_since",
                new_callable=AsyncMock,
                return_value=[pending],
            ),
            patch(
                "pynchy.host.orchestrator.messaging.host_controls.intercept_special_command",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            result = await intercept_immediate_checkpoint_controls(deps, "g@g.us", group, [pending])

        assert result is None

    @pytest.mark.asyncio
    async def test_reclassify_host_control_ignores_external_messages(self):
        group = _make_group()
        deps = _make_deps()
        message = _make_message(
            "!status",
            metadata={"authenticated_external_route": True},
        )

        assert await reclassify_host_control(deps, "g@g.us", group, message) is False

    @pytest.mark.asyncio
    async def test_reclassify_host_control_returns_false_when_interception_fails(self):
        group = _make_group()
        deps = _make_deps()
        message = _make_message("!status")

        with patch(
            "pynchy.host.orchestrator.messaging.host_controls.intercept_special_command",
            new_callable=AsyncMock,
            return_value=False,
        ):
            assert await reclassify_host_control(deps, "g@g.us", group, message) is False

    @pytest.mark.asyncio
    async def test_reclassify_host_control_marks_deferred_command_metadata(self):
        group = _make_group()
        deps = _make_deps()
        message = _make_message("pause")

        with patch(
            "pynchy.host.orchestrator.messaging.host_controls.mark_message_as_host",
            new_callable=AsyncMock,
        ) as mark_host:
            assert await reclassify_host_control(deps, "g@g.us", group, message) is True

        mark_host.assert_awaited_once_with(
            message.id,
            "g@g.us",
            deferred_control=True,
        )
        assert message.metadata == {"deferred_host_control": True}

    @pytest.mark.asyncio
    async def test_normal_message_not_intercepted(self):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("what's up?")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.commands.is_redeploy",
                return_value=False,
            ),
        ):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is False

    @pytest.mark.asyncio
    async def test_whitespace_stripped_before_checking(self):
        """Leading/trailing whitespace stripped before command check."""
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("  reset context  ")

        with patch(
            "pynchy.host.orchestrator.messaging.pipeline.commands.is_context_reset",
            return_value=True,
        ):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is True


class TestExecuteDirectCommand:
    _P_SHELL = "pynchy.host.orchestrator.messaging.direct_command.run_shell_command"

    @pytest.mark.asyncio
    async def test_successful_command_broadcasts_output(self, tmp_path):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!echo hi")
        deps.direct_command_workdir.return_value = tmp_path / "groups" / group.folder

        with (
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
        ):
            mock_shell.return_value = ShellResult(returncode=0, stdout="hi", stderr="")
            await execute_direct_command(deps, "g@g.us", group, msg, "echo hi")

        saved = deps.record_direct_command_output.await_args.args[0]
        assert saved.chat_jid == "g@g.us"
        assert saved.group == group
        assert saved.source_message == msg
        assert saved.command == "echo hi"
        assert saved.exit_code == 0
        assert saved.content == "✅ Command output (exit 0):\n```\nhi\n```"
        assert mock_shell.await_args.kwargs["cwd"] == str(tmp_path / "groups" / group.folder)
        deps.broadcast_to_channels.assert_awaited_once()
        event = deps.broadcast_to_channels.call_args[0][1]
        assert "✅" in event.content
        assert "hi" in event.content

    @pytest.mark.asyncio
    async def test_failed_command_shows_error(self, tmp_path):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!false")
        deps.direct_command_workdir.return_value = tmp_path / "groups" / group.folder

        with (
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
        ):
            mock_shell.return_value = ShellResult(returncode=1, stdout="", stderr="error msg")
            await execute_direct_command(deps, "g@g.us", group, msg, "false")

        saved = deps.record_direct_command_output.await_args.args[0]
        assert saved.exit_code == 1
        assert saved.content == "❌ Command output (exit 1):\n```\nerror msg\n```"
        event = deps.broadcast_to_channels.call_args[0][1]
        assert "❌" in event.content
        assert "error msg" in event.content

    @pytest.mark.asyncio
    async def test_timeout_sends_host_message(self, tmp_path):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!sleep 99")
        deps.direct_command_workdir.return_value = tmp_path / "groups" / group.folder

        with (
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
        ):
            mock_shell.return_value = ShellResult(
                returncode=None, stdout="", stderr="", timed_out=True
            )
            await execute_direct_command(deps, "g@g.us", group, msg, "sleep 99")

        deps.broadcast_host_message.assert_awaited_once()
        host_text = deps.broadcast_host_message.call_args[0][1]
        assert "timed out" in host_text.lower()

    @pytest.mark.asyncio
    async def test_command_start_failure_sends_host_message(self, tmp_path):
        group = _make_group()
        deps = _make_deps()
        deps.direct_command_workdir.return_value = tmp_path / "groups" / group.folder

        with patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell:
            mock_shell.return_value = ShellResult(
                returncode=None,
                stdout="",
                stderr="",
                start_error="working directory is unavailable",
            )
            await execute_direct_command(deps, "g@g.us", group, _make_message("!pwd"), "pwd")

        deps.broadcast_host_message.assert_awaited_once_with(
            "g@g.us", "❌ Command failed: working directory is unavailable"
        )
        deps.record_direct_command_output.assert_not_awaited()
