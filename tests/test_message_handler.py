"""Tests for message processing (message_handler) and routing (_message_routing).

Covers:
- intercept_special_command: reset, end session, redeploy, !commands
- process_group_messages: reset handoff, trigger filtering, cursor management,
  dirty repo check, error rollback, worktree merge
- _check_dirty_repo, advance_cursor, _handle_reset_handoff (extracted helpers)
- start_message_loop: "btw" non-interrupting messages during active tasks
"""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
from pynchy.host.orchestrator.messaging.inbound import start_message_loop
from pynchy.host.orchestrator.messaging.pipeline import (
    MessageHandlerDeps,
    execute_direct_command,
    intercept_special_command,
    process_group_messages,
)
from pynchy.types import ContainerOutput, NewMessage, WorkspaceProfile

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_SETTINGS = "pynchy.host.orchestrator.messaging.pipeline.get_settings"
_P_MSGS_SINCE = "pynchy.host.orchestrator.messaging.pipeline.get_messages_since"
_P_INTERCEPT = "pynchy.host.orchestrator.messaging.pipeline.intercept_special_command"
_P_FMT_SDK = "pynchy.host.orchestrator.messaging.formatter.format_messages_for_sdk"
_P_STORE = "pynchy.host.orchestrator.messaging.pipeline.store_message_direct"
_P_DIRTY = "pynchy.host.orchestrator.messaging.run_context.is_repo_dirty"
_P_GET_RA = "pynchy.host.orchestrator.workspace_config.get_repo_access"
_P_MERGE = "pynchy.host.git_ops._worktree_merge.merge_and_push_worktree"

# Patch paths for names imported in _message_routing (routing/loop tests).
_PR = "pynchy.host.orchestrator.messaging.inbound"
_PR_SETTINGS = f"{_PR}.get_settings"
_PR_NEW_MSGS = f"{_PR}.get_new_messages"
_PR_MSGS_SINCE = f"{_PR}.get_messages_since"
_PR_INTERCEPT = f"{_PR}.intercept_special_command"
_PR_BG_TASK = f"{_PR}.create_background_task"

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
    deps._dispatched_through = {}
    deps.last_timestamp = last_timestamp
    deps.channels = []  # empty by default; tests that need channel routing set this explicitly

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
    deps.run_agent = AsyncMock(return_value="success")
    deps.handle_streamed_output = AsyncMock(return_value=True)

    # Queue mock
    deps.queue = MagicMock()
    deps.queue.is_active_task = MagicMock(return_value=False)
    deps.queue.send_message = MagicMock(return_value=False)
    deps.queue.enqueue_message_check = MagicMock()
    deps.queue.clear_pending_tasks = MagicMock()
    deps.queue.stop_active_process = AsyncMock()
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
) -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid=chat_jid,
        sender=sender,
        sender_name=sender_name,
        content=content,
        timestamp=timestamp,
        is_from_me=is_from_me,
    )


def _patch_intercept(*, return_value: bool = False):
    return patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=return_value)


def _patch_msgs_since(messages: list):
    return patch(_P_MSGS_SINCE, new_callable=AsyncMock, return_value=messages)


def _patch_fmt_sdk():
    return patch(_P_FMT_SDK, return_value=[{"content": "hello"}])


def _patch_bg_task():
    """Patch create_background_task, closing coroutine args to avoid unawaited warnings."""

    def _cleanup(coro, *, name=None):
        if hasattr(coro, "close"):
            coro.close()

    return patch(_PR_BG_TASK, side_effect=_cleanup)


# ---------------------------------------------------------------------------
# intercept_special_command
# ---------------------------------------------------------------------------


class TestInterceptSpecialCommand:
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
        deps.handle_context_reset.assert_awaited_once_with("g@g.us", group, msg.timestamp)

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
        deps.handle_end_session.assert_awaited_once()

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
        deps.trigger_manual_redeploy.assert_awaited_once_with("g@g.us")
        assert deps.last_agent_timestamp["g@g.us"] == msg.timestamp
        deps.save_state.assert_awaited_once()

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
                "pynchy.host.orchestrator.messaging.pipeline.execute_direct_command",
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
    _P_SHELL = "pynchy.host.orchestrator.messaging.pipeline.run_shell_command"

    @pytest.mark.asyncio
    async def test_successful_command_broadcasts_output(self, tmp_path):
        from pynchy.utils import ShellResult

        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!echo hi")

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(_P_STORE, new_callable=AsyncMock),
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
        ):
            mock_settings.return_value.groups_dir = tmp_path / "groups"
            mock_shell.return_value = ShellResult(returncode=0, stdout="hi", stderr="")
            await execute_direct_command(deps, "g@g.us", group, msg, "echo hi")

        deps.broadcast_to_channels.assert_awaited_once()
        event = deps.broadcast_to_channels.call_args[0][1]
        assert "✅" in event.content
        assert "hi" in event.content

    @pytest.mark.asyncio
    async def test_failed_command_shows_error(self, tmp_path):
        from pynchy.utils import ShellResult

        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!false")

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(_P_STORE, new_callable=AsyncMock),
            patch(self._P_SHELL, new_callable=AsyncMock) as mock_shell,
        ):
            mock_settings.return_value.groups_dir = tmp_path / "groups"
            mock_shell.return_value = ShellResult(returncode=1, stdout="", stderr="error msg")
            await execute_direct_command(deps, "g@g.us", group, msg, "false")

        event = deps.broadcast_to_channels.call_args[0][1]
        assert "❌" in event.content
        assert "error msg" in event.content

    @pytest.mark.asyncio
    async def test_timeout_sends_host_message(self, tmp_path):
        from pynchy.utils import ShellResult

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
    async def test_cursor_rollback_on_save_state_failure(self, tmp_path):
        """save_state failure at completion → cursor rolls back to pre-run value."""
        group = _make_group(is_admin=True)
        deps = _make_deps(
            groups={"g@g.us": group},
            last_agent_ts={"g@g.us": "old-ts"},
        )
        deps.save_state = AsyncMock(side_effect=RuntimeError("DB failure"))
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            ms.return_value = _settings_mock(tmp_path)
            with pytest.raises(RuntimeError, match="DB failure"):
                await process_group_messages(deps, "g@g.us")

        # Cursor rolls back so the DB (which still has "old-ts") stays consistent
        # with in-memory state. Messages will be re-processed on the next trigger.
        assert deps.last_agent_timestamp["g@g.us"] == "old-ts"

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
        async def mock_run_agent(group, jid, msgs, on_output=None, notices=None):
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
    async def test_successful_run_triggers_worktree_merge(self, tmp_path):
        """pynchy_repo_access group → worktree merge triggered."""
        group = _make_group(is_admin=False)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.run_agent = AsyncMock(return_value="success")
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch("pynchy.host.git_ops._worktree_merge.background_merge_worktree") as mock_bg_merge,
        ):
            ms.return_value = _settings_mock(tmp_path)
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        mock_bg_merge.assert_called_once_with(group)

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

        msg1 = _make_message("hello", timestamp="ts-1")
        msg2 = _make_message("reset context", timestamp="ts-2")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg1, msg2]),
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
        observed["dispatched"] = deps._dispatched_through.get("g@g.us")
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
    from pynchy.config import AgentConfig, IntervalsConfig

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
    async def test_non_btw_message_interrupts_active_task(self):
        """A regular message (no 'btw' prefix) while a task runs should
        kill the task and clear pending tasks."""
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
            _patch_bg_task(),
        ):
            await _run_loop_once(deps)

        # Task IS interrupted
        deps.queue.clear_pending_tasks.assert_called_once_with(jid)
        deps.queue.stop_active_process.assert_called_once_with(jid)

    @pytest.mark.asyncio
    async def test_btw_without_space_interrupts_task(self):
        """'btwsomething' (no space after btw) should interrupt the task,
        since only 'btw ' (with trailing space) is the non-interrupting
        prefix."""
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
            _patch_bg_task(),
        ):
            await _run_loop_once(deps)

        # Should interrupt — "btw" without a space is a normal message
        deps.queue.clear_pending_tasks.assert_called_once_with(jid)
        deps.queue.stop_active_process.assert_called_once_with(jid)

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
            _patch_bg_task(),
        ):
            await _run_loop_once(deps)

        # Notice should reach the active container (interrupt path since
        # it's not a "btw" message)
        deps.queue.clear_pending_tasks.assert_called_once_with(jid)
        deps.queue.stop_active_process.assert_called_once_with(jid)

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
