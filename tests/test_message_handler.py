"""Tests for pynchy.message_handler — command interception, message processing,
and extracted helpers.

Covers:
- intercept_special_command: reset, end session, redeploy, !commands
- process_group_messages: reset handoff, trigger filtering, cursor management,
  dirty repo check, error rollback, worktree merge
- _check_dirty_repo, _advance_cursor, _handle_reset_handoff (extracted helpers)
- start_message_loop: "btw" non-interrupting messages during active tasks
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.message_handler import (
    _advance_cursor,
    _check_dirty_repo,
    _handle_reset_handoff,
    execute_direct_command,
    intercept_special_command,
    process_group_messages,
    start_message_loop,
)
from pynchy.types import NewMessage

# Commonly patched module paths — avoids repeating long strings and keeps
# line lengths under 100 chars.
_P_SETTINGS = "pynchy.message_handler.get_settings"
_P_MSGS_SINCE = "pynchy.message_handler.get_messages_since"
_P_NEW_MSGS = "pynchy.message_handler.get_new_messages"
_P_INTERCEPT = "pynchy.message_handler.intercept_special_command"
_P_FMT_SDK = "pynchy.router.format_messages_for_sdk"
_P_STORE = "pynchy.message_handler.store_message_direct"
_P_DIRTY = "pynchy.message_handler.is_repo_dirty"
_P_HAS_PA = "pynchy.workspace_config.has_project_access"
_P_MERGE = "pynchy.worktree.merge_and_push_worktree"
_P_BG_TASK = "pynchy.message_handler.create_background_task"

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
    deps = MagicMock()
    deps.registered_groups = groups or {}
    deps.last_agent_timestamp = last_agent_ts if last_agent_ts is not None else {}
    deps.last_timestamp = last_timestamp

    # Async helpers
    deps.save_state = AsyncMock()
    deps.handle_context_reset = AsyncMock()
    deps.handle_end_session = AsyncMock()
    deps.trigger_manual_redeploy = AsyncMock()
    deps.broadcast_to_channels = AsyncMock()
    deps.broadcast_host_message = AsyncMock()
    deps.send_reaction_to_channels = AsyncMock()
    deps.set_typing_on_channels = AsyncMock()
    deps.emit = MagicMock()
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
    is_god: bool = False,
    requires_trigger: bool | None = True,
) -> MagicMock:
    group = MagicMock()
    group.name = name
    group.folder = folder
    group.is_god = is_god
    group.requires_trigger = requires_trigger
    return group


def _make_message(
    content: str = "hello",
    *,
    id: str = "msg-1",
    chat_jid: str = "group@g.us",
    sender: str = "user@s.whatsapp.net",
    sender_name: str = "Alice",
    timestamp: str = "2024-01-01T00:00:01.000Z",
    is_from_me: bool | None = None,
) -> NewMessage:
    return NewMessage(
        id=id,
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

        with patch("pynchy.message_handler.is_context_reset", return_value=True):
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
                "pynchy.message_handler.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_end_session",
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
                "pynchy.message_handler.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_redeploy",
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
                "pynchy.message_handler.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_redeploy",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.execute_direct_command",
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
                "pynchy.message_handler.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_redeploy",
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
                "pynchy.message_handler.is_context_reset",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_end_session",
                return_value=False,
            ),
            patch(
                "pynchy.message_handler.is_redeploy",
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

        with patch("pynchy.message_handler.is_context_reset", return_value=True):
            result = await intercept_special_command(deps, "g@g.us", group, msg)

        assert result is True


# ---------------------------------------------------------------------------
# execute_direct_command
# ---------------------------------------------------------------------------


class TestExecuteDirectCommand:
    @pytest.mark.asyncio
    async def test_successful_command_broadcasts_output(self):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!echo hi")

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(_P_STORE, new_callable=AsyncMock),
            patch("subprocess.run") as mock_run,
        ):
            mock_settings.return_value.groups_dir = Path("/tmp/groups")
            mock_run.return_value = MagicMock(returncode=0, stdout="hi\n", stderr="")
            await execute_direct_command(deps, "g@g.us", group, msg, "echo hi")

        deps.broadcast_to_channels.assert_awaited_once()
        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "✅" in channel_text
        assert "hi" in channel_text

    @pytest.mark.asyncio
    async def test_failed_command_shows_error(self):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!false")

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(_P_STORE, new_callable=AsyncMock),
            patch("subprocess.run") as mock_run,
        ):
            mock_settings.return_value.groups_dir = Path("/tmp/groups")
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
            await execute_direct_command(deps, "g@g.us", group, msg, "false")

        channel_text = deps.broadcast_to_channels.call_args[0][1]
        assert "❌" in channel_text
        assert "error msg" in channel_text

    @pytest.mark.asyncio
    async def test_timeout_sends_host_message(self):
        group = _make_group()
        deps = _make_deps()
        msg = _make_message("!sleep 99")

        import subprocess as sp

        with (
            patch(_P_SETTINGS) as mock_settings,
            patch(
                "subprocess.run",
                side_effect=sp.TimeoutExpired("sleep", 30),
            ),
        ):
            mock_settings.return_value.groups_dir = Path("/tmp/groups")
            await execute_direct_command(deps, "g@g.us", group, msg, "sleep 99")

        deps.broadcast_host_message.assert_awaited_once()
        host_text = deps.broadcast_host_message.call_args[0][1]
        assert "timed out" in host_text.lower()


# ---------------------------------------------------------------------------
# process_group_messages
# ---------------------------------------------------------------------------


def _settings_mock(tmp_path, **overrides):
    """Create a Settings mock with common defaults."""
    m = MagicMock()
    m.data_dir = tmp_path
    m.trigger_pattern = MagicMock()
    m.idle_timeout = 300
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


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

        with patch(_P_SETTINGS) as ms:
            ms.return_value.data_dir = tmp_path
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

        with patch(_P_SETTINGS) as ms:
            ms.return_value.data_dir = tmp_path
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        assert (ipc_dir / "needs_dirty_check.json").exists()

    @pytest.mark.asyncio
    async def test_reset_handoff_malformed_json_handled(self, tmp_path):
        """Malformed reset_prompt.json handled gracefully."""
        group = _make_group()
        deps = _make_deps(groups={"g@g.us": group})

        ipc_dir = tmp_path / "ipc" / "test-group"
        ipc_dir.mkdir(parents=True)
        reset_file = ipc_dir / "reset_prompt.json"
        reset_file.write_text("NOT VALID JSON")

        with patch(_P_SETTINGS) as ms:
            ms.return_value.data_dir = tmp_path
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
            ms.return_value.data_dir = tmp_path
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_god_trigger_required_but_missing(self, tmp_path):
        """Non-god group, required trigger missing → skip."""
        group = _make_group(is_god=False, requires_trigger=True)
        deps = _make_deps(groups={"g@g.us": group})
        msg = _make_message("hello")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
        ):
            ms.return_value = _settings_mock(tmp_path)
            ms.return_value.trigger_pattern.search.return_value = None
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        deps.run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cursor_rollback_on_save_state_failure(self, tmp_path):
        """save_state failure → cursor rolls back."""
        group = _make_group(is_god=True)
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

        assert deps.last_agent_timestamp["g@g.us"] == "old-ts"

    @pytest.mark.asyncio
    async def test_agent_error_rolls_back_cursor(self, tmp_path):
        """Agent error → cursor rolled back, user notified."""
        group = _make_group(is_god=True)
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
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={"g@g.us": group},
            last_agent_ts={"g@g.us": "old-ts"},
        )
        msg = _make_message("hello", timestamp="new-ts")

        # run_agent invokes the on_output callback to simulate
        # output being sent before error.
        async def mock_run_agent(group, jid, msgs, on_output=None, notices=None):
            if on_output:
                output = MagicMock(type="result", result="hello", status="error")
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
        """project_access group → worktree merge triggered."""
        group = _make_group(is_god=False)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.run_agent = AsyncMock(return_value="success")
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
            patch(_P_HAS_PA, return_value=True),
            patch(_P_MERGE),
            patch(_P_BG_TASK) as mock_bg_task,
        ):
            ms.return_value = _settings_mock(tmp_path)
            ms.return_value.trigger_pattern.search.return_value = True
            result = await process_group_messages(deps, "g@g.us")

        assert result is True
        mock_bg_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_dirty_repo_warning_added_for_god_group(self, tmp_path):
        """Dirty repo after reset → system notice added."""
        group = _make_group(is_god=True)
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
        group = _make_group(is_god=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})
        deps.handle_streamed_output = AsyncMock(return_value=False)
        msg = _make_message("hello", timestamp="new-ts", id="msg-42")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg]),
            _patch_intercept(),
            _patch_fmt_sdk(),
        ):
            ms.return_value = _settings_mock(tmp_path)
            await process_group_messages(deps, "g@g.us")

        deps.send_reaction_to_channels.assert_awaited_once_with(
            "g@g.us", "msg-42", msg.sender, "👀"
        )
        assert deps.set_typing_on_channels.await_count == 2
        deps.set_typing_on_channels.assert_any_await("g@g.us", True)
        deps.set_typing_on_channels.assert_any_await("g@g.us", False)

    @pytest.mark.asyncio
    async def test_special_command_intercepts(self, tmp_path):
        """Special commands checked on the last message."""
        group = _make_group(is_god=True)
        deps = _make_deps(groups={"g@g.us": group}, last_agent_ts={})

        msg1 = _make_message("hello", timestamp="ts-1")
        msg2 = _make_message("reset context", timestamp="ts-2")

        with (
            patch(_P_SETTINGS) as ms,
            _patch_msgs_since([msg1, msg2]),
            _patch_intercept(return_value=True),
        ):
            ms.return_value.data_dir = tmp_path
            result = await process_group_messages(deps, "g@g.us")

        assert result is True


# ---------------------------------------------------------------------------
# _check_dirty_repo (extracted helper)
# ---------------------------------------------------------------------------


class TestCheckDirtyRepo:
    def test_no_file_returns_empty(self, tmp_path):
        """No dirty_check file → empty notices list."""
        marker = tmp_path / "needs_dirty_check.json"
        result = _check_dirty_repo("test-group", marker)
        assert result == []

    def test_clean_repo_returns_empty(self, tmp_path):
        """Marker file + clean repo → empty notices, file consumed."""
        marker = tmp_path / "needs_dirty_check.json"
        marker.write_text("{}")

        with patch(_P_DIRTY, return_value=False):
            result = _check_dirty_repo("test-group", marker)

        assert result == []
        assert not marker.exists()

    def test_dirty_repo_returns_warning(self, tmp_path):
        """Marker file + dirty repo → warning notice."""
        marker = tmp_path / "needs_dirty_check.json"
        marker.write_text("{}")

        with patch(_P_DIRTY, return_value=True):
            result = _check_dirty_repo("test-group", marker)

        assert len(result) == 1
        assert "uncommitted" in result[0].lower()
        assert not marker.exists()

    def test_exception_during_check_cleans_up(self, tmp_path):
        """Exception during check → file cleaned up, empty result."""
        marker = tmp_path / "needs_dirty_check.json"
        marker.write_text("{}")

        with patch(_P_DIRTY, side_effect=RuntimeError("git broken")):
            result = _check_dirty_repo("test-group", marker)

        assert result == []
        assert not marker.exists()


# ---------------------------------------------------------------------------
# _advance_cursor (extracted helper)
# ---------------------------------------------------------------------------


class TestAdvanceCursor:
    @pytest.mark.asyncio
    async def test_advances_and_persists(self):
        """Normal: cursor advances and save_state is called."""
        deps = _make_deps(last_agent_ts={"g@g.us": "old-ts"})
        previous = await _advance_cursor(deps, "g@g.us", "new-ts")

        assert previous == "old-ts"
        assert deps.last_agent_timestamp["g@g.us"] == "new-ts"
        deps.save_state.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rollback_on_save_failure(self):
        """save_state failure → cursor rolls back."""
        deps = _make_deps(last_agent_ts={"g@g.us": "old-ts"})
        deps.save_state = AsyncMock(side_effect=RuntimeError("DB error"))

        with pytest.raises(RuntimeError, match="DB error"):
            await _advance_cursor(deps, "g@g.us", "new-ts")

        assert deps.last_agent_timestamp["g@g.us"] == "old-ts"

    @pytest.mark.asyncio
    async def test_missing_cursor_defaults_to_empty(self):
        """No previous cursor → default to empty string."""
        deps = _make_deps(last_agent_ts={})
        previous = await _advance_cursor(deps, "g@g.us", "first-ts")

        assert previous == ""
        assert deps.last_agent_timestamp["g@g.us"] == "first-ts"


# ---------------------------------------------------------------------------
# _handle_reset_handoff (extracted helper)
# ---------------------------------------------------------------------------


class TestHandleResetHandoff:
    @pytest.mark.asyncio
    async def test_no_file_returns_none(self, tmp_path):
        """No reset_prompt.json → returns None (not handled)."""
        group = _make_group()
        deps = _make_deps()
        reset_file = tmp_path / "reset_prompt.json"

        result = await _handle_reset_handoff(deps, "g@g.us", group, reset_file)
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_file_runs_agent(self, tmp_path):
        """Valid reset prompt → agent runs with handoff message."""
        group = _make_group()
        deps = _make_deps()

        reset_file = tmp_path / "reset_prompt.json"
        reset_file.write_text(json.dumps({"message": "Continue after reset"}))

        with patch(_P_SETTINGS) as ms:
            ms.return_value.data_dir = tmp_path
            result = await _handle_reset_handoff(deps, "g@g.us", group, reset_file)

        assert result is True
        deps.run_agent.assert_awaited_once()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_empty_message_returns_true_without_agent(self, tmp_path):
        """Empty message → skip agent, return True."""
        group = _make_group()
        deps = _make_deps()

        reset_file = tmp_path / "reset_prompt.json"
        reset_file.write_text(json.dumps({"message": ""}))

        with patch(_P_SETTINGS) as ms:
            ms.return_value.data_dir = tmp_path
            result = await _handle_reset_handoff(deps, "g@g.us", group, reset_file)

        assert result is True
        deps.run_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_malformed_json_returns_true(self, tmp_path):
        """Malformed JSON → clean up and return True."""
        group = _make_group()
        deps = _make_deps()

        reset_file = tmp_path / "reset_prompt.json"
        reset_file.write_text("NOT VALID JSON")

        result = await _handle_reset_handoff(deps, "g@g.us", group, reset_file)

        assert result is True
        deps.run_agent.assert_not_awaited()
        assert not reset_file.exists()

    @pytest.mark.asyncio
    async def test_agent_error_returns_false(self, tmp_path):
        """Agent returning 'error' → returns False."""
        group = _make_group()
        deps = _make_deps()
        deps.run_agent = AsyncMock(return_value="error")

        reset_file = tmp_path / "reset_prompt.json"
        reset_file.write_text(json.dumps({"message": "Hello"}))

        with patch(_P_SETTINGS) as ms:
            ms.return_value.data_dir = tmp_path
            result = await _handle_reset_handoff(deps, "g@g.us", group, reset_file)

        assert result is False


# ---------------------------------------------------------------------------
# start_message_loop — "btw" non-interrupting messages during active tasks
# ---------------------------------------------------------------------------


def _loop_settings_mock():
    """Settings mock suitable for start_message_loop tests."""
    s = MagicMock()
    s.agent.name = "Pynchy"
    s.intervals.message_poll = 0  # no sleep between iterations
    s.trigger_pattern.search.return_value = True
    return s


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


class TestBtwNonInterruptingMessages:
    """Messages starting with 'btw' should not interrupt active tasks.

    They are forwarded via IPC (best-effort) and the group is marked for
    reprocessing after the task exits — but the task is NOT killed and the
    cursor is NOT advanced.
    """

    @pytest.mark.asyncio
    async def test_btw_message_does_not_interrupt_active_task(self):
        """A 'btw ...' message while a task runs should forward via IPC
        and mark pending, without killing the task."""
        jid = "group@g.us"
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # Simulate an active scheduled task
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("btw here's some extra context", timestamp="new-ts")

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # IPC forwarded (best-effort)
        deps.queue.send_message.assert_called_once_with(
            jid, "Alice: btw here's some extra context"
        )
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
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("BTW also check the logs", timestamp="new-ts")

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
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
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg = _make_message("  btw one more thing", timestamp="new-ts")

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        deps.queue.send_message.assert_called_once()
        deps.queue.stop_active_process.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_btw_message_interrupts_active_task(self):
        """A regular message (no 'btw' prefix) while a task runs should
        kill the task and clear pending tasks."""
        jid = "group@g.us"
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True

        msg = _make_message("do something else now", timestamp="new-ts")

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
            patch(_P_BG_TASK),
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
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True

        msg = _make_message("btwsomething", timestamp="new-ts")

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
            patch(_P_BG_TASK),
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
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        deps.queue.is_active_task.return_value = True
        deps.queue.send_message.return_value = True

        msg1 = _make_message(
            "do something urgent",
            id="msg-1",
            timestamp="ts-1",
        )
        msg2 = _make_message(
            "btw also consider this",
            id="msg-2",
            timestamp="ts-2",
        )

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg1, msg2], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg1, msg2],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
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
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # Not a scheduled task, but a message container IS active
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = True  # container is active

        msg = _make_message("btw here's some info", timestamp="new-ts")

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
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
    async def test_btw_routed_normally_when_no_active_container(self):
        """'btw ...' when no container is active at all should be routed
        normally — enqueued for a fresh container run."""
        jid = "group@g.us"
        group = _make_group(is_god=True)
        deps = _make_deps(
            groups={jid: group},
            last_agent_ts={jid: "old-ts"},
        )
        # No active container at all
        deps.queue.is_active_task.return_value = False
        deps.queue.send_message.return_value = False

        msg = _make_message("btw here's some info", timestamp="new-ts")

        with (
            patch(_P_SETTINGS, return_value=_loop_settings_mock()),
            patch(
                _P_NEW_MSGS,
                new_callable=AsyncMock,
                return_value=([msg], "poll-ts"),
            ),
            patch(
                _P_MSGS_SINCE,
                new_callable=AsyncMock,
                return_value=[msg],
            ),
            patch(_P_INTERCEPT, new_callable=AsyncMock, return_value=False),
        ):
            await _run_loop_once(deps)

        # Falls through to normal enqueue_message_check
        deps.queue.enqueue_message_check.assert_called_once_with(jid)
