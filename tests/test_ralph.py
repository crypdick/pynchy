"""Tests for the Ralph Wiggum loop.

Tests the iterative agent execution with LLM verification, including:
- Loop lifecycle (start, iterate, stop)
- Verifier prompt building and response parsing
- Stagnation detection
- Check command execution
- Worker message building with failure context
- IPC-triggered start/stop
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.ralph import (
    _build_verifier_prompt,
    _build_worker_messages,
    _is_stagnating,
    _parse_verifier_response,
    is_ralph_active,
    run_ralph_loop,
    stop_ralph,
)
from pynchy.types import RalphLoopConfig, RalphLoopState


@pytest.fixture
def ralph_config():
    return RalphLoopConfig(
        prompt="Fix all failing tests",
        check_command="pytest",
        max_iterations=5,
        session_mode="same",
        stagnation_threshold=3,
    )


@pytest.fixture
def ralph_state(ralph_config):
    return RalphLoopState(
        config=ralph_config,
        group_folder="test-group",
        chat_jid="test@g.us",
        iteration=1,
    )


class MockRalphDeps:
    """Mock implementation of RalphDeps protocol."""

    def __init__(self):
        self.worker_calls: list[dict] = []
        self.verifier_calls: list[str] = []
        self.host_messages: list[tuple[str, str]] = []
        self.cleared_sessions: list[str] = []
        self.worker_result = "success"
        self.verifier_response = "CONTINUE\nProgress being made"
        self.check_results: list[tuple[int, str]] = []  # (exit_code, output) per iteration

    async def run_worker_agent(
        self, group_folder, chat_jid, messages, on_output, extra_system_notices
    ):
        self.worker_calls.append(
            {
                "group_folder": group_folder,
                "chat_jid": chat_jid,
                "messages": messages,
                "system_notices": extra_system_notices,
            }
        )
        return self.worker_result

    async def run_verifier(self, prompt):
        self.verifier_calls.append(prompt)
        return self.verifier_response

    async def broadcast_host_message(self, chat_jid, text):
        self.host_messages.append((chat_jid, text))

    async def clear_session(self, group_folder):
        self.cleared_sessions.append(group_folder)


class TestParseVerifierResponse:
    """Test verifier response parsing — critical for correct loop control."""

    def test_continue_decision(self):
        response = "CONTINUE\nTests still failing but count dropped"
        decision, reasoning = _parse_verifier_response(response)
        assert decision == "CONTINUE"
        assert "still failing" in reasoning

    def test_stop_decision(self):
        decision, reasoning = _parse_verifier_response("STOP\nSame errors repeating")
        assert decision == "STOP"
        assert "Same errors" in reasoning

    def test_case_insensitive(self):
        decision, _ = _parse_verifier_response("continue\nsome reason")
        assert decision == "CONTINUE"

    def test_stop_with_extra_whitespace(self):
        decision, _ = _parse_verifier_response("  STOP  \n  reason  ")
        assert decision == "STOP"

    def test_empty_response_defaults_to_stop(self):
        decision, reasoning = _parse_verifier_response("")
        assert decision == "STOP"
        assert "Empty" in reasoning

    def test_unexpected_response_defaults_to_stop(self):
        decision, reasoning = _parse_verifier_response("MAYBE\nNot sure what to do")
        assert decision == "STOP"
        assert "Not sure" in reasoning

    def test_continue_no_reasoning(self):
        decision, reasoning = _parse_verifier_response("CONTINUE")
        assert decision == "CONTINUE"
        assert reasoning == ""

    def test_multiline_reasoning(self):
        response = "STOP\nFirst reason.\nSecond reason.\nThird."
        decision, reasoning = _parse_verifier_response(response)
        assert decision == "STOP"
        assert "First reason" in reasoning
        assert "Third" in reasoning


class TestStagnationDetection:
    """Test stagnation detection — prevents infinite loops with no progress."""

    def test_not_stagnating_below_threshold(self, ralph_state):
        ralph_state.recent_check_outputs = ["error1", "error2"]
        assert not _is_stagnating(ralph_state)

    def test_stagnating_identical_outputs(self, ralph_state):
        ralph_state.recent_check_outputs = [
            "FAILED: test_foo",
            "FAILED: test_foo",
            "FAILED: test_foo",
        ]
        assert _is_stagnating(ralph_state)

    def test_not_stagnating_different_outputs(self, ralph_state):
        ralph_state.recent_check_outputs = [
            "FAILED: 3 tests",
            "FAILED: 2 tests",
            "FAILED: 1 test",
        ]
        assert not _is_stagnating(ralph_state)

    def test_not_stagnating_empty_history(self, ralph_state):
        ralph_state.recent_check_outputs = []
        assert not _is_stagnating(ralph_state)

    def test_stagnation_only_checks_recent(self, ralph_state):
        """Only the last N outputs matter, not the full history."""
        ralph_state.recent_check_outputs = [
            "different error",
            "FAILED: test_foo",
            "FAILED: test_foo",
            "FAILED: test_foo",
        ]
        assert _is_stagnating(ralph_state)

    def test_custom_threshold(self, ralph_config):
        ralph_config.stagnation_threshold = 2
        state = RalphLoopState(
            config=ralph_config,
            group_folder="test-group",
            chat_jid="test@g.us",
            recent_check_outputs=["same", "same"],
        )
        assert _is_stagnating(state)


class TestBuildVerifierPrompt:
    """Test verifier prompt construction."""

    def test_includes_task_prompt(self, ralph_config, ralph_state):
        prompt = _build_verifier_prompt(ralph_config, ralph_state, 1, "test failed")
        assert "Fix all failing tests" in prompt

    def test_includes_check_command(self, ralph_config, ralph_state):
        prompt = _build_verifier_prompt(ralph_config, ralph_state, 1, "test failed")
        assert "pytest" in prompt

    def test_includes_exit_code(self, ralph_config, ralph_state):
        prompt = _build_verifier_prompt(ralph_config, ralph_state, 42, "test failed")
        assert "42" in prompt

    def test_includes_check_output(self, ralph_config, ralph_state):
        check_output = "FAILED test_foo.py::test_bar"
        prompt = _build_verifier_prompt(ralph_config, ralph_state, 1, check_output)
        assert check_output in prompt

    def test_includes_iteration_info(self, ralph_config, ralph_state):
        ralph_state.iteration = 3
        prompt = _build_verifier_prompt(ralph_config, ralph_state, 1, "err")
        assert "Iteration 3" in prompt
        assert "5" in prompt  # max_iterations

    def test_includes_history_when_available(self, ralph_config, ralph_state):
        ralph_state.recent_check_outputs = ["error 1", "error 2"]
        prompt = _build_verifier_prompt(ralph_config, ralph_state, 1, "error 3")
        assert "error 1" in prompt
        assert "error 2" in prompt

    def test_first_iteration_marker(self, ralph_config, ralph_state):
        ralph_state.recent_check_outputs = []
        prompt = _build_verifier_prompt(ralph_config, ralph_state, 1, "error")
        assert "first iteration" in prompt


class TestBuildWorkerMessages:
    """Test worker message construction across iterations."""

    def test_first_iteration_only_prompt(self, ralph_config, ralph_state):
        messages = _build_worker_messages(ralph_config, ralph_state)
        assert len(messages) == 1
        assert messages[0]["content"] == "Fix all failing tests"
        assert messages[0]["metadata"]["source"] == "ralph_loop"

    def test_subsequent_iteration_includes_failure(self, ralph_config, ralph_state):
        ralph_state.iteration = 2
        ralph_state.recent_check_outputs = ["FAILED: test_foo\nAssertionError"]
        messages = _build_worker_messages(ralph_config, ralph_state)
        assert len(messages) == 2
        assert "FAILED: test_foo" in messages[1]["content"]
        assert "still failing" in messages[1]["content"]

    def test_failure_context_truncation(self, ralph_config, ralph_state):
        ralph_state.iteration = 2
        ralph_state.recent_check_outputs = ["x" * 10000]
        messages = _build_worker_messages(ralph_config, ralph_state)
        assert len(messages[1]["content"]) < 10000
        assert "truncated" in messages[1]["content"]


class TestRunRalphLoop:
    """Test the main loop orchestration — the core Ralph Wiggum behavior."""

    @pytest.mark.asyncio
    async def test_check_passes_first_iteration(self, ralph_config):
        """Should stop immediately if check passes on first try."""
        deps = MockRalphDeps()

        with patch("pynchy.ralph._run_check_command", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (0, "All tests passed")

            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "success"
        assert result["iterations"] == 1
        assert len(deps.worker_calls) == 1
        assert len(deps.verifier_calls) == 0  # No verifier needed when check passes

    @pytest.mark.asyncio
    async def test_check_passes_after_several_iterations(self, ralph_config):
        """Should iterate until check passes."""
        deps = MockRalphDeps()
        deps.verifier_response = "CONTINUE\nProgress being made"
        call_count = 0

        async def mock_check(cmd, folder):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:
                return (0, "All tests passed")
            return (1, f"FAILED: {4 - call_count} tests")

        with patch("pynchy.ralph._run_check_command", side_effect=mock_check):
            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "success"
        assert result["iterations"] == 3
        assert len(deps.worker_calls) == 3
        assert len(deps.verifier_calls) == 2  # Called on iterations 1 and 2

    @pytest.mark.asyncio
    async def test_verifier_stops_loop(self, ralph_config):
        """Should stop when verifier says STOP."""
        deps = MockRalphDeps()
        deps.verifier_response = "STOP\nSame errors, no progress"

        with patch("pynchy.ralph._run_check_command", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (1, "FAILED: test_foo")

            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "verifier_stop"
        assert result["iterations"] == 1
        assert len(deps.worker_calls) == 1
        assert len(deps.verifier_calls) == 1

    @pytest.mark.asyncio
    async def test_stagnation_halts_loop(self, ralph_config):
        """Should halt when same check output repeats."""
        deps = MockRalphDeps()
        deps.verifier_response = "CONTINUE\nKeep trying"

        with patch("pynchy.ralph._run_check_command", new_callable=AsyncMock) as mock_check:
            # Same output every time → stagnation after threshold (3)
            mock_check.return_value = (1, "FAILED: test_foo identical output")

            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "stagnated"
        # Stagnation detected on iteration 3 (threshold=3)
        assert result["iterations"] == 3

    @pytest.mark.asyncio
    async def test_max_iterations_exhausted(self, ralph_config):
        """Should stop after max iterations."""
        ralph_config.max_iterations = 3
        ralph_config.stagnation_threshold = 100  # Disable stagnation
        deps = MockRalphDeps()
        deps.verifier_response = "CONTINUE\nKeep going"
        call_count = 0

        async def mock_check(cmd, folder):
            nonlocal call_count
            call_count += 1
            return (1, f"FAILED: error {call_count}")

        with patch("pynchy.ralph._run_check_command", side_effect=mock_check):
            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "max_iterations"
        assert result["iterations"] == 3

    @pytest.mark.asyncio
    async def test_manual_stop(self, ralph_config):
        """Should stop when stop_ralph is called."""
        deps = MockRalphDeps()

        async def mock_check(cmd, folder):
            # Stop the loop during the check
            stop_ralph("test-group")
            return (1, "FAILED")

        with patch("pynchy.ralph._run_check_command", side_effect=mock_check):
            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_fresh_session_mode_clears_between_iterations(self, ralph_config):
        """Should clear session between iterations in fresh mode."""
        ralph_config.session_mode = "fresh"
        ralph_config.max_iterations = 2
        ralph_config.stagnation_threshold = 100
        deps = MockRalphDeps()
        deps.verifier_response = "CONTINUE\nKeep going"
        call_count = 0

        async def mock_check(cmd, folder):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return (0, "passed")
            return (1, "FAILED")

        with patch("pynchy.ralph._run_check_command", side_effect=mock_check):
            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "success"
        # Session should have been cleared before iteration 2
        assert "test-group" in deps.cleared_sessions

    @pytest.mark.asyncio
    async def test_worker_error_continues_to_check(self, ralph_config):
        """Worker errors shouldn't abort the loop — check might still pass."""
        deps = MockRalphDeps()
        deps.worker_result = "error"

        with patch("pynchy.ralph._run_check_command", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (0, "All tests passed")

            result = await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert result["status"] == "success"
        assert result["iterations"] == 1

    @pytest.mark.asyncio
    async def test_broadcasts_status_messages(self, ralph_config):
        """Should broadcast status messages to the user."""
        deps = MockRalphDeps()

        with patch("pynchy.ralph._run_check_command", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (0, "All tests passed")

            await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        # Should have broadcast: start, iteration running, check running, completion
        messages = [msg for _, msg in deps.host_messages]
        assert any("started" in m.lower() for m in messages)
        assert any("complete" in m.lower() or "passed" in m.lower() for m in messages)

    @pytest.mark.asyncio
    async def test_loop_state_cleanup_on_completion(self, ralph_config):
        """Active loop state should be cleaned up when loop finishes."""
        deps = MockRalphDeps()

        with patch("pynchy.ralph._run_check_command", new_callable=AsyncMock) as mock_check:
            mock_check.return_value = (0, "passed")

            assert not is_ralph_active("test-group")
            await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)
            assert not is_ralph_active("test-group")

    @pytest.mark.asyncio
    async def test_loop_state_cleanup_on_error(self, ralph_config):
        """Active loop state should be cleaned up even on error."""
        deps = MockRalphDeps()

        with patch("pynchy.ralph._run_check_command", side_effect=Exception("boom")):
            await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert not is_ralph_active("test-group")

    @pytest.mark.asyncio
    async def test_system_notices_on_subsequent_iterations(self, ralph_config):
        """Worker should get system notices about the ralph loop context on iteration 2+."""
        deps = MockRalphDeps()
        deps.verifier_response = "CONTINUE\nKeep going"
        call_count = 0

        async def mock_check(cmd, folder):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                return (0, "passed")
            return (1, "FAILED")

        with patch("pynchy.ralph._run_check_command", side_effect=mock_check):
            await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        # First iteration: no system notices
        assert deps.worker_calls[0]["system_notices"] is None
        # Second iteration: should have ralph loop context notice
        assert deps.worker_calls[1]["system_notices"] is not None
        assert "iteration 2" in deps.worker_calls[1]["system_notices"][0].lower()


class TestIsRalphActive:
    """Test active loop tracking."""

    def test_inactive_by_default(self):
        assert not is_ralph_active("nonexistent-group")

    @pytest.mark.asyncio
    async def test_active_during_loop(self, ralph_config):
        """Should report active while loop is running."""
        deps = MockRalphDeps()
        active_during_check = False

        async def mock_check(cmd, folder):
            nonlocal active_during_check
            active_during_check = is_ralph_active("test-group")
            return (0, "passed")

        with patch("pynchy.ralph._run_check_command", side_effect=mock_check):
            await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert active_during_check


class TestStopRalph:
    """Test manual stop signaling."""

    def test_stop_nonexistent_returns_false(self):
        assert not stop_ralph("nonexistent-group")

    @pytest.mark.asyncio
    async def test_stop_active_returns_true(self, ralph_config):
        """Should return True when stopping an active loop."""
        deps = MockRalphDeps()
        stop_result = False

        async def mock_check(cmd, folder):
            nonlocal stop_result
            stop_result = stop_ralph("test-group")
            return (1, "FAILED")

        with patch("pynchy.ralph._run_check_command", side_effect=mock_check):
            await run_ralph_loop(ralph_config, "test-group", "test@g.us", deps)

        assert stop_result


class TestRunCheckCommand:
    """Test check command execution."""

    @pytest.mark.asyncio
    async def test_successful_command(self, tmp_path):
        from pynchy.ralph import _run_check_command

        with patch("pynchy.ralph._get_check_cwd", return_value=tmp_path):
            exit_code, output = await _run_check_command("echo hello", "test-group")

        assert exit_code == 0
        assert "hello" in output

    @pytest.mark.asyncio
    async def test_failing_command(self, tmp_path):
        from pynchy.ralph import _run_check_command

        with patch("pynchy.ralph._get_check_cwd", return_value=tmp_path):
            exit_code, output = await _run_check_command("exit 1", "test-group")

        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_timeout_handling(self, tmp_path):
        from pynchy.ralph import _run_check_command

        with (
            patch("pynchy.ralph._get_check_cwd", return_value=tmp_path),
            patch("pynchy.ralph.RALPH_CHECK_TIMEOUT", 0.01),
        ):
            exit_code, output = await _run_check_command("sleep 10", "test-group")

        assert exit_code == 1
        assert "timed out" in output.lower()
