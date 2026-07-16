"""End-to-end interactive-turn coverage for the deterministic runtime."""

from __future__ import annotations

import subprocess  # noqa: S404, RUF100 - tests inspect the harness-owned Docker container only.
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from ._helpers import (
    groups,
    messages,
    response_requests,
    runtime_state,
    send_message,
    status,
    wait_for_ready,
    wait_for_response_count,
    wait_for_response_request,
)

pytestmark = pytest.mark.runtime


@pytest.mark.timeout(180)
def test_interactive_container_turn_uses_gateway_and_reuses_its_ipc_session() -> None:
    """A cold and warm TUI turn cross every deterministic runtime boundary."""
    state = runtime_state()
    jid = _tui_jid(state)
    response_text = _required_string(state, "response_text")
    before = _response_count(messages(state, jid), response_text)
    cold_marker = uuid4().hex
    warm_marker = uuid4().hex

    send_message(state, jid, f"runtime cold container turn {cold_marker}")
    first_history = wait_for_response_count(state, jid, response_text, before + 1)
    cold_request = wait_for_response_request(state, cold_marker)
    first_container_id = _container_id(state)
    _assert_container_has_no_github_token(state)

    send_message(state, jid, f"runtime warm container turn {warm_marker}")
    second_history = wait_for_response_count(state, jid, response_text, before + 2)
    warm_request = wait_for_response_request(state, warm_marker)

    assert _container_id(state) == first_container_id
    assert warm_request["previous_response_id"] == cold_request["response_id"]
    assert warm_request["response_id"] != cold_request["response_id"]
    assert any(
        item.get("content") == f"runtime cold container turn {cold_marker}"
        for item in first_history
    )
    assert any(
        item.get("content") == f"runtime warm container turn {warm_marker}"
        for item in second_history
    )

    temporal = status(state)["temporal"]
    assert temporal["last_task_id"] == jid
    assert temporal["last_result"] == "completed"
    assert isinstance(temporal["last_workflow_id"], str)
    assert temporal["last_completed_at"] is not None


@pytest.mark.timeout(180)
def test_runtime_restart_preserves_history_and_accepts_a_fresh_turn() -> None:
    """A harness restart keeps SQLite history and restores the interactive path."""
    state = runtime_state()
    jid = _tui_jid(state)
    response_text = _required_string(state, "response_text")
    before = _response_count(messages(state, jid), response_text)
    before_marker = uuid4().hex
    after_marker = uuid4().hex

    send_message(state, jid, f"runtime before restart {before_marker}")
    before_restart_history = wait_for_response_count(state, jid, response_text, before + 1)
    before_restart_request = wait_for_response_request(state, before_marker)

    restart_started = time.monotonic()
    restart = subprocess.run(  # noqa: S603, RUF100 - fixed repository-local harness command.
        [  # noqa: S607, RUF100 - uv is the repository's required Python runner.
            "uv",
            "run",
            "python",
            "scripts/runtime_harness.py",
            "restart",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
    )
    assert restart.returncode == 0
    assert time.monotonic() - restart_started < 45

    restarted_state = runtime_state()
    wait_for_ready(restarted_state)
    persisted_history = messages(restarted_state, jid)
    assert any(
        item.get("content") == f"runtime before restart {before_marker}"
        for item in persisted_history
    )
    assert len(persisted_history) >= len(before_restart_history)

    send_message(restarted_state, jid, f"runtime after restart {after_marker}")
    after_restart_history = wait_for_response_count(
        restarted_state,
        jid,
        response_text,
        before + 2,
    )
    after_restart_request = wait_for_response_request(restarted_state, after_marker)
    assert after_restart_request["previous_response_id"] == before_restart_request["response_id"]
    assert after_restart_request["response_id"] != before_restart_request["response_id"]
    assert any(
        item.get("content") == f"runtime after restart {after_marker}"
        for item in after_restart_history
    )
    assert status(restarted_state)["temporal"]["last_result"] == "completed"


@pytest.mark.timeout(180)
def test_interactive_container_turn_executes_a_scripted_patch_task() -> None:
    """A deterministic user request exercises model tool selection and workspace writes."""
    state = runtime_state()
    jid = _tui_jid(state)
    response_text = "PYNCHY_RUNTIME_PATCH_OK"
    proof_path = (
        Path(__file__).resolve().parents[2] / "groups" / "pynchy" / "runtime-patch-proof.txt"
    )
    proof_path.unlink(missing_ok=True)
    before = _response_count(messages(state, jid), response_text)
    marker = uuid4().hex

    try:
        send_message(state, jid, f"PYNCHY_RUNTIME_PATCH_PROBE {marker}: create the proof file.")
        history = wait_for_response_count(state, jid, response_text, before + 1)
        request = wait_for_response_request(state, marker)

        assert proof_path.read_text(encoding="utf-8") == "PYNCHY_RUNTIME_PATCH_OK"
        assert request["previous_response_id"] is not None
        assert any(item.get("content") == response_text for item in history)
    finally:
        proof_path.unlink(missing_ok=True)


@pytest.mark.timeout(180)
def test_interactive_container_turn_executes_a_scripted_shell_task() -> None:
    """A deterministic shell request writes only the group's mounted workspace."""
    state = runtime_state()
    jid = _tui_jid(state)
    response_text = "PYNCHY_RUNTIME_SHELL_OK"
    proof_path = (
        Path(__file__).resolve().parents[2] / "groups" / "pynchy" / "runtime-shell-proof.txt"
    )
    proof_path.unlink(missing_ok=True)
    before = _response_count(messages(state, jid), response_text)
    marker = uuid4().hex

    try:
        send_message(state, jid, f"PYNCHY_RUNTIME_SHELL_PROBE {marker}: write the proof file.")
        history = wait_for_response_count(state, jid, response_text, before + 1)
        request = wait_for_response_request(state, marker)
        tool_result = next(
            item
            for item in response_requests(state)
            if item.get("previous_response_id") == request["response_id"]
        )

        assert proof_path.read_text(encoding="utf-8") == response_text
        assert request["previous_response_id"] is not None
        assert "call_runtime_shell_probe" in str(tool_result["input"])
        assert any(item.get("content") == response_text for item in history)
    finally:
        proof_path.unlink(missing_ok=True)


def _tui_jid(state: dict[str, Any]) -> str:
    matching = [group.get("jid") for group in groups(state) if group.get("folder") == "pynchy"]
    assert matching == ["tui://pynchy"]
    return "tui://pynchy"


def _response_count(history: list[dict[str, Any]], response_text: str) -> int:
    return sum(
        item.get("sender_name") == "pynchy" and item.get("content") == response_text
        for item in history
    )


def _container_name(state: dict[str, Any]) -> str:
    return f"{_required_string(state, 'namespace')}-pynchy"


def _container_id(state: dict[str, Any]) -> str:
    result = subprocess.run(  # noqa: S603, RUF100 - name is generated by the harness state.
        [  # noqa: S607, RUF100 - Docker is a required local runtime executable.
            "docker",
            "inspect",
            "--format",
            "{{.Id}}",
            _container_name(state),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0
    container_id = result.stdout.strip()
    assert container_id
    return container_id


def _assert_container_has_no_github_token(state: dict[str, Any]) -> None:
    """Prove the actual sourced container environment has no ambient GH_TOKEN."""
    result = subprocess.run(  # noqa: S603, RUF100 - fixed shell test against a harness-owned container.
        [  # noqa: S607, RUF100 - Docker is a required local runtime executable.
            "docker",
            "exec",
            _container_name(state),
            "sh",
            "-c",
            'test -z "${GH_TOKEN+x}"',
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0


def _required_string(state: dict[str, Any], key: str) -> str:
    value = state.get(key)
    assert isinstance(value, str)
    return value
