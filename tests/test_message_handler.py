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
import re
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from conftest import make_settings

from pynchy.config import AgentConfig, IntervalsConfig
from pynchy.config.models import LearningConfig
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
from pynchy.host.orchestrator import session_handler
from pynchy.host.orchestrator.messaging.cursor import (
    complete_turn_with_cursor as persist_completed_turn,
)
from pynchy.host.orchestrator.messaging.inbound import start_message_loop
from pynchy.host.orchestrator.messaging.outcomes import TURN_PAUSED
from pynchy.host.orchestrator.messaging.pipeline import (
    CONTINUE_AFTER_SAFE_INTERRUPT,
    MessageHandlerDeps,
    execute_direct_command,
    intercept_special_command,
    process_group_messages,
)
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    begin_in_flight_turn,
    claim_next_conversation_delivery,
    get_chat_history,
    get_conversation_delivery,
    get_in_flight_turn,
    get_in_flight_turn_for_chat,
    init_test_database,
    prepare_in_flight_turn_recovery,
    store_message,
)
from pynchy.state import get_messages_since as get_stored_messages_since
from pynchy.types import (
    CheckpointControlState,
    ContainerOutput,
    GroupFolder,
    InFlightTurn,
    InFlightWorkKind,
    NewMessage,
    WorkspaceProfile,
)
from pynchy.utils import ShellResult

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_SETTINGS = "pynchy.host.orchestrator.messaging.pipeline.get_settings"
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.pipeline.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"
_P_DIRTY = "pynchy.host.orchestrator.messaging.run_context.is_repo_dirty"
_P_GET_RA = "pynchy.host.orchestrator.workspace_config.get_repo_access"

# Patch paths for names imported in _message_routing (routing/loop tests).
_PR = "pynchy.host.orchestrator.messaging.inbound"
_PR_SETTINGS = f"{_PR}.get_settings"
_PR_NEW_MSGS = f"{_PR}.get_new_messages"
_PR_MSGS_SINCE = f"{_PR}.get_messages_since"
_PR_INTERCEPT = f"{_PR}.intercept_special_command"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_deps(
    *,
    groups: dict | None = None,
    last_agent_ts: dict | None = None,
    last_timestamp: str = "",
) -> MagicMock:
    """Build a MessageHandlerDeps mock with sensible defaults."""
    deps = MagicMock(spec=MessageHandlerDeps)
    deps.workspaces = groups or {}
    deps.last_agent_timestamp = last_agent_ts if last_agent_ts is not None else {}
    dispatched_through = {}
    deps.last_timestamp = last_timestamp
    deps.channels = []  # empty by default; tests that need channel routing set this explicitly
    deps.routing_cursor = MagicMock(
        side_effect=lambda jid: max(
            deps.last_agent_timestamp.get(jid, ""),
            dispatched_through.get(jid, ""),
        )
    )
    deps.mark_dispatched = MagicMock(
        side_effect=lambda jid, timestamp: dispatched_through.__setitem__(
            jid,
            max(dispatched_through.get(jid, ""), timestamp),
        )
    )
    deps.pop_dispatched = MagicMock(side_effect=dispatched_through.pop)
    deps.dispatched_timestamp = MagicMock(side_effect=dispatched_through.get)

    # Async helpers
    deps.save_state = AsyncMock()
    deps.handle_context_reset = AsyncMock()
    deps.handle_end_session = AsyncMock()
    deps.trigger_manual_redeploy = AsyncMock()
    deps.broadcast_to_channels = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    deps.send_reaction_to_channels = AsyncMock()
    deps.send_reaction_to_outbound = AsyncMock()
    deps.processing_ack_emoji = MagicMock(return_value="🦞")
    deps.set_typing_on_channels = AsyncMock()
    deps.emit = MagicMock()
    deps.start_interactive_turn = AsyncMock()
    deps.start_interrupted_turn = AsyncMock()
    deps.run_agent = AsyncMock(return_value="success")
    deps.handle_streamed_output = AsyncMock(return_value=True)

    # Queue mock
    deps.queue = MagicMock()
    deps.queue.is_active_task = MagicMock(return_value=False)
    deps.queue.send_message = MagicMock(return_value=False)
    deps.queue.enqueue_message_check = MagicMock()
    deps.queue.clear_pending_tasks = MagicMock()
    deps.queue.stop_active_process = AsyncMock()
    deps.queue.stop_active_process_for_control = AsyncMock()
    deps.queue.has_active_run = MagicMock(return_value=False)
    deps.queue.interrupt_after_tool_result = AsyncMock(return_value=False)
    deps.queue.close_stdin = MagicMock()

    return deps


def _make_group(
    *,
    name: str = "test-group",
    folder: str = "test-group",
    is_admin: bool = False,
) -> MagicMock:
    group = MagicMock(spec=WorkspaceProfile)
    group.name = name
    group.folder = folder
    group.is_admin = is_admin
    return group


def _make_message(
    content: str = "hello",
    *,
    message_id: str = "msg-1",
    chat_jid: str = "group@g.us",
    sender: str = "user@s.whatsapp.net",
    sender_name: str = "Alice",
    timestamp: str = "2024-01-01T00:00:01.000Z",
    is_from_me: bool | None = None,
    metadata: dict[str, object] | None = None,
) -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        is_from_me=is_from_me,
        metadata=metadata,
    )


async def _claimed_external_message(
    jid: str,
    group: WorkspaceProfile,
    *,
    suffix: str,
    provider: str = "matrix",
    public_source_input: bool | None = None,
) -> tuple[NewMessage, ExternalDeliveryIdentity]:
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider(provider),
        route=ExternalRoute("personal:family"),
        delivery_id=ExternalDeliveryId(f"$event-{suffix}"),
    )
    await admit_external_delivery_receipt(
        ExternalDeliveryReceipt(
            identity=identity,
            payload_sha256=f"sha-{suffix}",
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
    claim_id = ConversationClaimId(f"claim-{suffix}")
    assert await claim_next_conversation_delivery(admission.conversation.id, claim_id)
    metadata: dict[str, object] = {
        "authenticated_external_route": True,
        "external_provider": provider,
        "conversation_claim_id": claim_id,
    }
    if public_source_input is not None:
        metadata["public_source_input"] = public_source_input
    message = _make_message(
        f"external input {suffix}",
        message_id=str(identity.delivery_id),
        chat_jid=jid,
        sender="@stranger:matrix.example.com",
        timestamp="2026-07-19T12:00:01+00:00",
        metadata=metadata,
    )
    await store_message(message)
    return message, identity


def _patch_intercept(*, return_value: bool = False):
    return patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=return_value)


def _patch_msgs_since(messages: list):
    return patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=messages)


def _patch_fmt_sdk():
    return patch(_P_FMT_SDK, return_value=[{"content": "hello"}])


# ---------------------------------------------------------------------------
# intercept_special_command
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

        with patch(
            "pynchy.host.orchestrator.messaging.host_controls.destroy_session",
            new_callable=AsyncMock,
        ) as destroy:
            assert await intercept_special_command(deps, jid, group, msg) is True

        checkpoint = await get_in_flight_turn("turn-pause")
        assert checkpoint is not None
        assert checkpoint.control_state is CheckpointControlState.PAUSE_REQUESTED
        assert checkpoint.session_id == "provider-thread"
        history = await get_chat_history(jid)
        assert next(message for message in history if message.id == msg.id).message_type == "host"
        assert deps.last_agent_timestamp[jid] == msg.timestamp
        deps.queue.stop_active_process_for_control.assert_awaited_once_with(jid)
        destroy.assert_awaited_once_with(group.folder)
        deps.broadcast_host_message.assert_awaited_once_with(jid, "⏸️")

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

        with patch(
            "pynchy.host.orchestrator.messaging.host_controls.destroy_session",
            new_callable=AsyncMock,
        ):
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

        with (
            patch(
                "pynchy.host.orchestrator.session_handler._send_command_confirmation",
                new_callable=AsyncMock,
            ) as confirmation,
            patch(
                "pynchy.host.orchestrator.session_handler.get_head_sha",
                return_value="a" * 40,
            ),
            patch(
                "pynchy.host.orchestrator.session_handler.start_deploy_workflow",
                new_callable=AsyncMock,
            ) as start_deploy,
        ):
            await session_handler.trigger_manual_redeploy(deps, "g@g.us", source_message=msg)

        confirmation.assert_awaited_once_with(deps, "g@g.us", msg, "🔄")
        request = start_deploy.await_args.args[0]
        assert not hasattr(request, "active_sessions")

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


# ---------------------------------------------------------------------------
# execute_direct_command
# ---------------------------------------------------------------------------


class TestExecuteDirectCommand:
    _P_SHELL = "pynchy.host.orchestrator.messaging.direct_command.run_shell_command"
    _P_STORE = "pynchy.host.orchestrator.messaging.direct_command.store_message_direct"

    @pytest.mark.asyncio
    async def test_successful_command_broadcasts_output(self, tmp_path):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!echo hi")

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
            patch(self._P_STORE, new_callable=AsyncMock) as mock_store,
        ):
            mock_settings.return_value.groups_dir = tmp_path / "groups"
            mock_shell.return_value = ShellResult(returncode=0, stdout="hi", stderr="")
            await execute_direct_command(deps, "g@g.us", group, msg, "echo hi")

        saved = mock_store.await_args.kwargs
        assert saved["chat_jid"] == "g@g.us"
        assert saved["sender"] == "command_output"
        assert saved["sender_name"] == "command"
        assert saved["content"] == "✅ Command output (exit 0):\n```\nhi\n```"
        assert saved["message_type"] == "host"
        assert saved["metadata"]["source_message_id"] == msg.id
        assert saved["metadata"]["source"] == "direct_command"
        assert saved["metadata"]["command"] == "echo hi"
        assert saved["metadata"]["exit_code"] == 0
        deps.broadcast_to_channels.assert_awaited_once()
        event = deps.broadcast_to_channels.call_args[0][1]
        assert "✅" in event.content
        assert "hi" in event.content

    @pytest.mark.asyncio
    async def test_failed_command_shows_error(self, tmp_path):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!false")

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
            patch(self._P_STORE, new_callable=AsyncMock) as mock_store,
        ):
            mock_settings.return_value.groups_dir = tmp_path / "groups"
            mock_shell.return_value = ShellResult(returncode=1, stdout="", stderr="error msg")
            await execute_direct_command(deps, "g@g.us", group, msg, "false")

        saved = mock_store.await_args.kwargs
        assert saved["content"] == "❌ Command output (exit 1):\n```\nerror msg\n```"
        assert saved["metadata"]["exit_code"] == 1
        event = deps.broadcast_to_channels.call_args[0][1]
        assert "❌" in event.content
        assert "error msg" in event.content

    @pytest.mark.asyncio
    async def test_timeout_sends_host_message(self, tmp_path):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!sleep 99")

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
        ):
            mock_settings.return_value.groups_dir = tmp_path / "groups"
            mock_shell.return_value = ShellResult(
                returncode=None, stdout="", stderr="", timed_out=True
            )
            await execute_direct_command(deps, "g@g.us", group, msg, "sleep 99")

        deps.broadcast_host_message.assert_awaited_once()
        host_text = deps.broadcast_host_message.call_args[0][1]
        assert "timed out" in host_text.lower()


# ---------------------------------------------------------------------------
# process_group_messages
# ---------------------------------------------------------------------------


def _settings_mock(tmp_path, **overrides):
    """Create a real Settings instance with common test defaults.

    trigger_pattern matches everything (equivalent to the old MagicMock
    stand-in's always-truthy .search()) so trigger-gating tests are unaffected.
    """
    defaults = {
        "data_dir": tmp_path,
        "learning": LearningConfig(enabled=False),
        "trigger_pattern": re.compile(r".*"),
        "idle_timeout": 300,
    }
    defaults.update(overrides)
    return make_settings(**defaults)


class TestProcessGroupMessages:
    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_returns_true_for_unknown_group(self):
        """Unknown group JID should return True (skip)."""
        deps = _make_deps(groups={})
        result = await process_group_messages(deps, "unknown@g.us")
        assert result is True

    @pytest.mark.asyncio
    async def test_reset_handoff_file_processed(self, tmp_path):
        """reset_prompt.json consumed → agent invoked."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})

        ipc_dir = tmp_path / "ipc" / "test-group"
        ipc_dir.mkdir(parents=True)
        reset_file = ipc_dir / "reset_prompt.json"
        reset_file.write_text(json.dumps({"message": "Hello after reset"}))

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_awaited_once()
        assert not reset_file.exists()

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
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        assert (ipc_dir / "needs_dirty_check.json").exists()

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
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_not_awaited()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_no_messages_returns_true(self, tmp_path):
        """No pending messages → early return True."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.approval_handler."
                "handle_approval_command",
                new_callable=AsyncMock,
            ) as handle_approval,
        ):
            assert await process_group_messages(deps, jid) is True

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
        assert deps.last_agent_timestamp[jid] == approval.timestamp

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

        async def run_agent(*_args, **_kwargs):
            agent_entered.set()
            await release_agent.wait()
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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
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
            assert await processing is True

        delivery = await get_conversation_delivery(identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == approval.timestamp

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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
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
            assert await processing is False

            delivery = await get_conversation_delivery(identity)
            assert delivery is not None
            assert delivery.status is ConversationDeliveryStatus.CLAIMED
            assert delivery.claim_id == external.metadata["conversation_claim_id"]
            assert not deps.last_agent_timestamp.get(jid, "")

            deps.run_agent = AsyncMock(return_value="success")
            assert await process_group_messages(deps, jid) is True

        handle_approval.assert_awaited_once()
        retried_delivery = await get_conversation_delivery(identity)
        assert retried_delivery is not None
        assert retried_delivery.status is ConversationDeliveryStatus.COMPLETED
        retried_messages = deps.run_agent.await_args.args[2]
        assert [message["content"] for message in retried_messages] == [external.content]
        assert deps.last_agent_timestamp[jid] == approval.timestamp

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

        async def run_agent(*_args, **_kwargs):
            agent_entered.set()
            await release_agent.wait()
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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
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
            assert await processing is True

        handle_approval.assert_awaited_once()
        deps.queue.send_message.assert_called_once_with(
            jid,
            f"{follow_up.sender_name}: {follow_up.content}",
        )
        assert "approve" not in deps.queue.send_message.call_args.args[1]
        delivery = await get_conversation_delivery(identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == follow_up.timestamp

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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
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
            assert await processing is True
            await routing

        handle_approval.assert_awaited_once()
        deps.start_interactive_turn.assert_not_awaited()
        delivery = await get_conversation_delivery(identity)
        assert delivery is not None
        assert delivery.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == approval.timestamp

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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
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
            assert await recovery_queue is True
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
            return "success"

        deps.run_agent.side_effect = run_agent

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_fmt_sdk(),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, jid)

        assert result is True
        deps.queue.interrupt_after_tool_result.assert_awaited_once_with(jid)

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
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_fmt_sdk(),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.complete_turn_with_cursor",
                new=complete_cursor,
            ),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, jid)

        assert result is CONTINUE_AFTER_SAFE_INTERRUPT
        assert completed == [(jid, "current-ts")]
        assert deps.pop_dispatched.call_args.args == (jid, "current-ts")

    @pytest.mark.asyncio
    async def test_non_admin_without_trigger_still_runs(self, tmp_path):
        """Workspace config no longer gates non-admin runs on mention triggers."""
        group = _make_group(is_admin=False)
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message("hello")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
        ):
            ms.return_value = _settings_mock(tmp_path, trigger_pattern=re.compile(r"(?!)"))
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
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
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(_PR_NEW_MSGS, new_callable=AsyncMock, return_value=([msg], "poll-ts")),
            patch(_PR_MSGS_SINCE, new_callable=AsyncMock, return_value=[msg]),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(jid)
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
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(
                "pynchy.host.orchestrator.messaging.cursor.complete_in_flight_turn",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB failure"),
            ),
        ):
            ms.return_value = _settings_mock(tmp_path)
            with pytest.raises(RuntimeError, match="DB failure"):
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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=[external]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            assert await process_group_messages(deps, jid) is True

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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
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
            assert await process_group_messages(deps, jid) is True

        completed = await get_conversation_delivery(identity)
        assert completed is not None
        assert completed.status is ConversationDeliveryStatus.COMPLETED
        assert deps.run_agent.await_args.kwargs["input_source"] == "external:matrix"
        assert deps.last_agent_timestamp[jid] == external.timestamp

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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
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
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            _patch_msgs_since([]),
        ):
            assert await process_group_messages(deps, jid) is True

        completed = await get_conversation_delivery(identity)
        assert completed is not None
        assert completed.status is ConversationDeliveryStatus.COMPLETED
        assert deps.last_agent_timestamp[jid] == external.timestamp

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
            patch(_P_SETTINGS) as ms,
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                side_effect=[[msg], []],
            ),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            ms.return_value = _settings_mock(tmp_path)
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
            assert await process_group_messages(deps, jid) is True

        assert len(recovered_messages) == 1
        recovery_prompt = recovered_messages[0]
        assert recovery_prompt["metadata"]["interrupted_turn_id"] == original_turn_id
        assert "continue the unfinished job" in recovery_prompt["content"]
        assert "Do not repeat it" in recovery_prompt["content"]
        assert (
            await get_in_flight_turn_for_chat(
                jid,
                {InFlightWorkKind.INTERACTIVE},
            )
            is None
        )
        assert deps.last_agent_timestamp[jid] == "new-ts"

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

        with (
            patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)),
            patch(
                "pynchy.host.orchestrator.messaging.host_controls.destroy_session",
                new_callable=AsyncMock,
            ),
        ):
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
            assert await processing is TURN_PAUSED

        checkpoint = await get_in_flight_turn_for_chat(
            jid,
            {InFlightWorkKind.INTERACTIVE},
        )
        assert checkpoint is not None
        assert checkpoint.control_state is CheckpointControlState.PAUSED
        assert checkpoint.claimed_at is None
        assert checkpoint.input_end_cursor == original.timestamp
        assert deps.last_agent_timestamp[jid] == pause.timestamp
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

        with patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)):
            assert await process_group_messages(deps, jid) is True

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
        assert deps.last_agent_timestamp[jid] == guidance.timestamp

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
                scheduled_base_chat_jid=jid,
                scheduled_thread_slot=2,
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

        with patch(_P_SETTINGS, return_value=_settings_mock(tmp_path)):
            assert await process_group_messages(deps, jid) is True

        deps.run_agent.assert_not_awaited()
        deps.start_interrupted_turn.assert_awaited_once_with("scheduled-paused", jid)
        checkpoint = await get_in_flight_turn("scheduled-paused")
        assert checkpoint is not None
        assert checkpoint.control_state is CheckpointControlState.ACTIVE
        assert checkpoint.claimed_at is None
        assert checkpoint.session_id == "scheduled-provider-thread"
        assert checkpoint.scheduled_base_chat_jid == jid
        assert checkpoint.scheduled_thread_slot == 2
        assert checkpoint.input_end_cursor == guidance.timestamp
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
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is False
        assert deps.last_agent_timestamp["g@g.us"] == "old-ts"
        deps.broadcast_host_message.assert_awaited_once()
        host_text = deps.broadcast_host_message.call_args[0][1]
        assert "error" in host_text.lower()

    @pytest.mark.asyncio
    async def test_agent_error_after_output_sent_no_rollback(self, tmp_path):
        """Agent error after output was sent → no rollback."""
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={"g@g.us": group},
            last_agent_ts={"g@g.us": "old-ts"},
        )
        msg = _make_message("hello", timestamp="new-ts")

        # run_agent invokes the on_output callback to simulate
        # output being sent before error.
        async def mock_run_agent(group, jid, msgs, on_output=None, notices=None, **_kwargs):
            if on_output:
                output = ContainerOutput(type="result", result="hello", status="error")
                await on_output(output)
            return "error"

        deps.run_agent = AsyncMock(side_effect=mock_run_agent)
        deps.handle_streamed_output = AsyncMock(return_value=True)

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        assert deps.last_agent_timestamp["g@g.us"] == "new-ts"
        deps.broadcast_host_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dirty_repo_warning_added_for_admin_group(self, tmp_path):
        """Dirty repo after reset → system notice added."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})

        ipc_dir = tmp_path / "ipc" / "test-group"
        ipc_dir.mkdir(parents=True)
        dirty_check = ipc_dir / "needs_dirty_check.json"
        dirty_check.write_text(json.dumps({"timestamp": "2024-01-01T00:00:00Z"}))
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_DIRTY, return_value=True),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        call_args = deps.run_agent.call_args
        # system_notices is the 5th positional arg
        notices = (
            call_args[0][4] if len(call_args[0]) > 4 else call_args[1].get("extra_system_notices")
        )
        assert notices is not None
        assert any("uncommitted" in n.lower() for n in notices)
        assert not dirty_check.exists()

    @pytest.mark.asyncio
    async def test_reaction_and_typing_indicator_sent(self, tmp_path):
        """Processing messages sends reaction and typing indicator."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts", message_id="msg-42")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        deps.send_reaction_to_channels.assert_awaited_once_with(
            "g@g.us", "msg-42", msg.sender, "🦞"
        )
        assert deps.set_typing_on_channels.await_count == 2
        deps.set_typing_on_channels.assert_any_await("g@g.us", is_typing=True)
        deps.set_typing_on_channels.assert_any_await("g@g.us", is_typing=False)

    @pytest.mark.asyncio
    async def test_custom_processing_ack_emoji_used_when_configured(self, tmp_path):
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        deps.processing_ack_emoji.return_value = "👀"
        msg = _make_message("hello", timestamp="new-ts", message_id="msg-42")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        deps.send_reaction_to_channels.assert_awaited_once_with(
            "g@g.us", "msg-42", msg.sender, "👀"
        )

    @pytest.mark.asyncio
    async def test_processing_ack_skipped_when_channel_disables_it(self, tmp_path):
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        deps.processing_ack_emoji.return_value = None
        msg = _make_message("hello", timestamp="new-ts", message_id="msg-42")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        deps.send_reaction_to_channels.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_only_does_not_launch_agent(self, tmp_path):
        """System notices alone shouldn't launch a container."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})

        notice = _make_message(
            "[System Notice] Auto-rebased 1 commit(s) onto your worktree.",
            sender="system_notice",
            sender_name="System",
            timestamp="new-ts",
        )

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([notice]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_plus_user_message_launches_agent(self, tmp_path):
        """A mix of system notices and user messages should launch the agent."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)

        notice = _make_message(
            "[System Notice] Auto-rebased 1 commit(s) onto your worktree.",
            message_id="notice-1",
            sender="system_notice",
            sender_name="System",
            timestamp="ts-1",
        )
        user_msg = _make_message(
            "hello",
            message_id="msg-1",
            sender="user@s.whatsapp.net",
            sender_name="Alice",
            timestamp="ts-2",
        )

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([notice, user_msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_special_command_intercepts(self, tmp_path):
        """Special commands checked on the last message."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})

        msg2 = _make_message("reset context", timestamp="ts-2")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg2]),
            _patch_intercept(return_value=True),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True

    @pytest.mark.asyncio
    async def test_zzz_reaction_registered_on_session_idle_callback(self, tmp_path):
        """After a successful agent run that produced output, the zzz
        reaction should be registered as the session's idle callback
        (fired when the container actually hibernates, not immediately)."""
        jid = "g@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={jid: group}, last_agent_ts={jid: "old-ts"})

        msg = _make_message("hello", message_id="msg-42", timestamp="new-ts")

        # run_agent must invoke the on_output callback so output_sent_to_user
        # is set to True inside process_group_messages.
        fake_result = ContainerOutput(status="success")

        async def _run_agent_with_callback(_group, _jid, _msgs, on_output=None, *a, **kw):
            if on_output:
                await on_output(fake_result)
            return "success"

        deps.run_agent = AsyncMock(side_effect=_run_agent_with_callback)
        deps.handle_streamed_output = AsyncMock(return_value=True)
        deps.send_reaction_to_outbound = AsyncMock()

        fake_ids = {"slack": "1234567890.000001"}
        mock_session = MagicMock()

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(
                "pynchy.host.orchestrator.messaging.pipeline.pop_last_result_ids",
                return_value=fake_ids,
            ),
            patch(
                "pynchy.host.container_manager.session.get_session",
                return_value=mock_session,
            ),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, jid)

        assert result is True
        # The reaction should NOT be sent immediately
        deps.send_reaction_to_outbound.assert_not_awaited()
        # Instead, set_idle_callback should be called on the session
        mock_session.set_idle_callback.assert_called_once()

        # Calling the stored callback should send the zzz reaction
        callback = mock_session.set_idle_callback.call_args[0][0]
        await callback()
        deps.send_reaction_to_outbound.assert_awaited_once_with(jid, fake_ids, "zzz")


# ---------------------------------------------------------------------------
# Dirty-repo notices (observed through process_group_messages)
# ---------------------------------------------------------------------------


def _dirty_notice_present(deps) -> bool:
    """True if run_agent received an 'uncommitted changes' system notice."""
    notices = deps.run_agent.call_args[0][4]
    return notices is not None and any("uncommitted" in n.lower() for n in notices)


class TestCheckDirtyRepo:
    """After a context reset the pipeline drops a needs_dirty_check.json marker.

    On the next run process_group_messages checks the repo and, if dirty,
    prepends an 'uncommitted changes' system notice for the agent. The marker
    is always consumed, and a check failure must not crash the run.
    """

    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_no_marker_no_notice(self, tmp_path):
        """No marker file → no dirty notice passed to the agent."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        assert not _dirty_notice_present(deps)

    @pytest.mark.asyncio
    async def test_clean_repo_no_notice_marker_consumed(self, tmp_path):
        """Marker file + clean repo → no notice, marker consumed."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        marker = tmp_path / "ipc" / "test-group" / "needs_dirty_check.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_DIRTY, return_value=False),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        assert not _dirty_notice_present(deps)
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_dirty_repo_adds_notice_marker_consumed(self, tmp_path):
        """Marker file + dirty repo → warning notice, marker consumed."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        marker = tmp_path / "ipc" / "test-group" / "needs_dirty_check.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_DIRTY, return_value=True),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        assert _dirty_notice_present(deps)
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_oserror_during_check_consumes_marker(self, tmp_path):
        """OSError during the dirty check → no crash, marker consumed, no notice."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        marker = tmp_path / "ipc" / "test-group" / "needs_dirty_check.json"
        marker.parent.mkdir(parents=True)
        marker.write_text("{}")
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_DIRTY, side_effect=OSError("permission denied")),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            # Should not raise
            await process_group_messages(deps, "g@g.us")

        assert not _dirty_notice_present(deps)
        assert not marker.exists()


# ---------------------------------------------------------------------------
# Dispatch tracking (observed through process_group_messages)
# ---------------------------------------------------------------------------


def _observe_at_run(deps):
    """Install a run_agent side effect that snapshots state at dispatch time.

    process_group_messages marks the batch dispatched *before* invoking the
    agent and only advances/persists the cursor after it returns — so a
    run_agent side effect observes the in-flight dispatch state directly.
    """
    observed: dict = {}

    async def _capture(*_args, **_kwargs):
        await asyncio.sleep(0)
        observed["dispatched"] = deps.dispatched_timestamp("g@g.us")
        observed["cursor"] = deps.last_agent_timestamp.get("g@g.us")
        observed["saves"] = deps.save_state.await_count
        return "success"

    deps.run_agent = AsyncMock(side_effect=_capture)
    return observed


async def _run_with_observer(tmp_path, deps):
    msg = _make_message("hello", timestamp="new-ts")
    with (
        patch(_P_SETTINGS) as ms,
        _patch_msgs_since([msg]),
        _patch_intercept(),
        _patch_fmt_sdk(),
        patch(_P_GET_RA, return_value=None),
    ):
        ms.return_value = _settings_mock(tmp_path)
        await process_group_messages(deps, "g@g.us")


class TestMarkDispatched:
    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_records_dispatched_timestamp(self, tmp_path):
        """The furthest message timestamp is recorded in-memory before the run."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        observed = _observe_at_run(deps)

        await _run_with_observer(tmp_path, deps)

        assert observed["dispatched"] == "new-ts"

    @pytest.mark.asyncio
    async def test_does_not_advance_cursor_before_completion(self, tmp_path):
        """last_agent_timestamp is untouched at dispatch — it advances only after."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        observed = _observe_at_run(deps)

        await _run_with_observer(tmp_path, deps)

        assert observed["cursor"] == "old-ts"

    @pytest.mark.asyncio
    async def test_does_not_save_state_before_run(self, tmp_path):
        """Dispatch tracking is in-memory only — no DB write before the run."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={"g@g.us": "old-ts"})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        observed = _observe_at_run(deps)

        await _run_with_observer(tmp_path, deps)

        assert observed["saves"] == 0


# ---------------------------------------------------------------------------
# Reset handoff (observed through process_group_messages)
# ---------------------------------------------------------------------------


def _reset_file(tmp_path):
    """Path to the reset_prompt.json the pipeline looks for (folder=test-group)."""
    path = tmp_path / "ipc" / "test-group" / "reset_prompt.json"
    path.parent.mkdir(parents=True)
    return path


class TestHandleResetHandoff:
    """process_group_messages consumes an agent-written reset_prompt.json before
    handling normal traffic: a valid prompt runs a handoff turn, an empty/absent
    prompt falls through, a malformed prompt is discarded, and a handoff error
    signals GroupQueue to retry (process returns False).
    """

    @pytest.fixture(autouse=True)
    async def _isolated_turn_ledger(self):
        await init_test_database()

    @pytest.mark.asyncio
    async def test_no_reset_file_processes_normally(self, tmp_path):
        """No reset_prompt.json → falls through to normal message processing."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_GET_RA, return_value=None),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_valid_reset_runs_handoff(self, tmp_path):
        """Valid reset prompt → handoff agent runs, file consumed."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        reset_file = _reset_file(tmp_path)
        reset_file.write_text(json.dumps({"message": "Continue after reset"}))

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_awaited_once()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_empty_message_skips_agent(self, tmp_path):
        """Empty reset message → no handoff run, file consumed, run continues."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        reset_file = _reset_file(tmp_path)
        reset_file.write_text(json.dumps({"message": ""}))

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_not_awaited()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_malformed_json_falls_through(self, tmp_path):
        """Malformed reset_prompt.json → discarded, normal processing proceeds."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        reset_file = _reset_file(tmp_path)
        reset_file.write_text("NOT VALID JSON")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_not_awaited()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_handoff_agent_error_signals_retry(self, tmp_path):
        """Handoff agent returning 'error' → process returns False (retry)."""
        group = _make_group(is_admin=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.run_agent = AsyncMock(return_value="error")
        reset_file = _reset_file(tmp_path)
        reset_file.write_text(json.dumps({"message": "Hello"}))

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is False


# ---------------------------------------------------------------------------
# start_message_loop — "btw" non-interrupting messages during active tasks
# ---------------------------------------------------------------------------


def _loop_settings_mock():
    """Settings instance suitable for start_message_loop tests."""
    return make_settings(
        agent=AgentConfig(name="Pynchy"),
        intervals=IntervalsConfig(message_poll=0.0),  # no sleep between iterations
        trigger_pattern=re.compile(r".*"),
    )


def _run_loop_once(deps):
    """Run start_message_loop for exactly one iteration, then stop."""
    call_count = 0

    def shutting_down():
        nonlocal call_count
        call_count += 1
        # Let the loop body execute once (first check returns False),
        # then stop on the next check (returns True).
        return call_count > 1

    return start_message_loop(deps, shutting_down)


@pytest.mark.asyncio
async def test_message_loop_does_not_run_channel_reconciliation_locally():
    """Channel reconciliation is Temporal-scheduled, not message-loop work."""
    deps = _make_deps()
    deps.catch_up_channels = AsyncMock()

    with (
        patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
        patch(_PR_NEW_MSGS, new_callable=AsyncMock, return_value=([], "")),
    ):
        await _run_loop_once(deps)

    deps.catch_up_channels.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_arriving_with_pause_is_queued_instead_of_sent_to_dead_ipc():
    await init_test_database()
    jid = "group@g.us"
    group = _make_group(is_admin=True)
    deps = _make_deps(groups={jid: group})
    deps.queue.has_active_run.return_value = True
    deps.queue.send_message.return_value = True
    await begin_in_flight_turn(
        InFlightTurn(
            turn_id="turn-pausing-with-reply",
            chat_jid=jid,
            group_folder=group.folder,
            work_kind=InFlightWorkKind.INTERACTIVE,
            input_messages=[{"sender_name": "Alice", "content": "original work"}],
            input_start_cursor="",
            input_end_cursor="2026-07-25T10:00:00+00:00",
            started_at="2026-07-25T10:00:00+00:00",
            claimed_at="2026-07-25T10:00:00+00:00",
        )
    )
    pause = _make_message(
        "pause",
        message_id="pause-race",
        chat_jid=jid,
        timestamp="2026-07-25T10:00:01+00:00",
    )
    guidance = _make_message(
        "Continue without publishing.",
        message_id="guidance-race",
        chat_jid=jid,
        timestamp="2026-07-25T10:00:02+00:00",
    )
    await store_message(pause)
    await store_message(guidance)

    with (
        patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
        patch(
            _PR_NEW_MSGS,
            new_callable=AsyncMock,
            return_value=([pause, guidance], guidance.timestamp),
        ),
        patch(
            "pynchy.config.access.filter_allowed_messages",
            side_effect=lambda messages, *_args: messages,
        ),
        patch(
            "pynchy.host.orchestrator.messaging.host_controls.destroy_session",
            new_callable=AsyncMock,
        ),
    ):
        await _run_loop_once(deps)

    deps.queue.send_message.assert_not_called()
    deps.queue.enqueue_message_check.assert_called_once_with(jid)
    checkpoint = await get_in_flight_turn("turn-pausing-with-reply")
    assert checkpoint is not None
    assert checkpoint.control_state is CheckpointControlState.PAUSE_REQUESTED
    assert deps.last_agent_timestamp[jid] == pause.timestamp


class TestBtwNonInterruptingMessages:
    """Messages starting with 'btw' should not interrupt active tasks.

    They are forwarded via IPC (best-effort) and the group is marked for
    reprocessing after the task exits — but the task is NOT killed and the
    cursor is NOT advanced.
    """

    @pytest.fixture(autouse=True)
    def _allow_all_senders(self, monkeypatch):
        """Route sender checks through permissive test settings."""
        settings = make_settings()
        monkeypatch.setattr("pynchy.config.access.get_settings", lambda: settings)

    @pytest.mark.asyncio
    async def test_btw_message_does_not_interrupt_active_task(self):
        """A 'btw ...' message while a task runs should forward via IPC
        and mark pending, without killing the task."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # Simulate an active scheduled task
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("btw here's some extra context", timestamp="new-ts")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # IPC forwarded (best-effort)
        deps.queue.send_message.assert_called_once_with(jid, "Alice: btw here's some extra context")
        # Marked for reprocessing after task exits
        deps.queue.enqueue_message_check.assert_called_once_with(jid)

        # Task NOT interrupted
        deps.queue.stop_active_process.assert_not_awaited()
        deps.queue.clear_pending_tasks.assert_not_called()

        # Cursor NOT advanced
        assert deps.last_agent_timestamp.get(jid) == "old-ts"

    @pytest.mark.asyncio
    async def test_btw_case_insensitive(self):
        """'BTW ...' (uppercase) should also be non-interrupting."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("BTW also check the logs", timestamp="new-ts")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Still forwarded, not interrupted
        deps.queue.send_message.assert_called_once()
        deps.queue.enqueue_message_check.assert_called_once()
        deps.queue.stop_active_process.assert_not_awaited()
        deps.queue.clear_pending_tasks.assert_not_called()

    @pytest.mark.asyncio
    async def test_btw_with_leading_whitespace(self):
        """'  btw ...' with leading whitespace should be non-interrupting
        (content is stripped before prefix check)."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("  btw one more thing", timestamp="new-ts")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.send_message.assert_called_once()
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_btw_message_defers_interrupt_until_tool_result(self):
        """A regular message queues a boundary interruption and clears pending tasks."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True

        msg = _make_message("do something else now", timestamp="new-ts")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.clear_pending_tasks.assert_called_once_with(jid)
        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(jid)
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_btw_without_space_defers_interrupt_until_tool_result(self):
        """'btwsomething' queues the same boundary interruption as normal input."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True

        msg = _make_message("btwsomething", timestamp="new-ts")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.clear_pending_tasks.assert_called_once_with(jid)
        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(jid)
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_btw_only_checked_on_last_message(self):
        """When multiple messages are pending, only the last one's content
        determines whether the batch is 'btw' (non-interrupting) or not."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg1 = _make_message(
            "do something urgent",
            message_id="msg-1",
            timestamp="ts-1",
        )
        msg2 = _make_message(
            "btw also consider this",
            message_id="msg-2",
            timestamp="ts-2",
        )

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg1, msg2], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg1, msg2],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Last message starts with "btw " → non-interrupting path
        deps.queue.send_message.assert_called_once()
        deps.queue.enqueue_message_check.assert_called_once()
        deps.queue.stop_active_process.assert_not_awaited()
        deps.queue.clear_pending_tasks.assert_not_called()

        # Formatted text sent to IPC should include both messages
        ipc_text = deps.queue.send_message.call_args[0][1]
        assert "do something urgent" in ipc_text
        assert "btw also consider this" in ipc_text

    @pytest.mark.asyncio
    async def test_btw_non_interrupting_during_message_processing(self):
        """'btw ...' while the agent is processing messages (not a task)
        should forward via IPC but not advance the cursor — the message
        is queued for reprocessing after the agent's turn ends."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # Not a scheduled task, but a message container IS active
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = True  # container is active

        msg = _make_message("btw here's some info", timestamp="new-ts")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # IPC forwarded
        deps.queue.send_message.assert_called_once()
        # Marked for reprocessing after agent turn ends
        deps.queue.enqueue_message_check.assert_called_once_with(jid)
        # Cursor NOT advanced
        assert deps.last_agent_timestamp.get(jid) == "old-ts"
        # No reaction sent (non-interrupting, will be reprocessed)
        deps.send_reaction_to_channels.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_only_does_not_wake_sleeping_agent(self):
        """System notices alone shouldn't enqueue a message check when
        no container is active — the agent stays asleep."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        notice = _make_message(
            "[System Notice] Auto-rebased 3 commit(s) onto your worktree.",
            sender="system_notice",
            sender_name="System",
            timestamp="new-ts",
        )

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([notice], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[notice],
            ),
        ):
            await _run_loop_once(deps)

        # Agent NOT woken up
        deps.queue.enqueue_message_check.assert_not_called()
        deps.queue.send_message.assert_not_called()
        # Cursor NOT advanced (notice will be included in next real session)
        assert deps.last_agent_timestamp.get(jid) == "old-ts"

    @pytest.mark.asyncio
    async def test_system_notice_forwarded_to_active_container(self):
        """System notices SHOULD be forwarded when a container is already
        active — the agent is awake and should see the notice."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        notice = _make_message(
            "[System Notice] Auto-rebased 3 commit(s) onto your worktree.",
            sender="system_notice",
            sender_name="System",
            timestamp="new-ts",
        )

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([notice], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[notice],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # The notice queues delivery at the active turn's next tool boundary.
        deps.queue.clear_pending_tasks.assert_called_once_with(jid)
        deps.queue.defer_interrupt_until_tool_result.assert_called_once_with(jid)
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_system_notice_with_user_message_wakes_agent(self):
        """A system notice mixed with a real user message should wake
        the agent normally."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        notice = _make_message(
            "[System Notice] Auto-rebased 1 commit(s).",
            message_id="notice-1",
            sender="system_notice",
            sender_name="System",
            timestamp="ts-1",
        )
        user_msg = _make_message(
            "hello",
            message_id="msg-1",
            sender="user@s.whatsapp.net",
            sender_name="Alice",
            timestamp="ts-2",
        )

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([notice, user_msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[notice, user_msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Agent SHOULD be woken up because there's a real user message.
        deps.start_interactive_turn.assert_awaited_once_with(jid)
        deps.queue.enqueue_message_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_btw_routed_normally_when_no_active_container(self):
        """'btw ...' when no container is active at all should be routed
        normally — enqueued for a fresh container run."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # No active container at all
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        msg = _make_message("btw here's some info", timestamp="new-ts")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Falls through to normal Temporal wake-up.
        deps.start_interactive_turn.assert_awaited_once_with(jid)
        deps.queue.enqueue_message_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_sunrise_reaction_on_wake(self):
        """When a message wakes a sleeping workspace, the first message
        in the batch should get a :sunrise: reaction."""
        jid = "group@g.us"
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        msg1 = _make_message("hello", message_id="msg-1", timestamp="ts-1")
        msg2 = _make_message("world", message_id="msg-2", timestamp="ts-2")

        with (
            patch(_PR_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _PR_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg1, msg2], "poll-ts"),
            ),
            patch(
                _PR_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg1, msg2],
            ),
            patch(_PR_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Only the first message gets sunrise
        deps.send_reaction_to_channels.assert_awaited_once_with(
            jid, "msg-1", msg1.sender, "sunrise"
        )
        # Still starts the durable turn
        deps.start_interactive_turn.assert_awaited_once_with(jid)
        deps.queue.enqueue_message_check.assert_not_called()
