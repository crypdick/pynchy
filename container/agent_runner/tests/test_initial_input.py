"""Tests for read_initial_input() — reads ContainerInput from initial.json."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_runner.main import read_initial_input
from agent_runner.models import ContainerInput


@pytest.fixture()
def input_dir(tmp_path: Path) -> Path:
    """Create a temporary IPC input directory."""
    d = tmp_path / "input"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def _patch_initial_input_file(input_dir: Path) -> None:
    """Redirect INITIAL_INPUT_FILE to the temp input dir."""
    with patch("agent_runner.main.INITIAL_INPUT_FILE", input_dir / "initial.json"):
        yield


def _minimal_input(**overrides: object) -> dict:
    """Return a minimal valid ContainerInput dict, with optional overrides."""
    base = {
        "messages": [{"content": "hello", "sender_name": "Alice", "timestamp": "2026-01-01"}],
        "group_folder": "test-group",
        "chat_jid": "test@chat.jid",
        "is_admin": False,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReadInitialInput:
    """read_initial_input() reads and parses initial.json correctly."""

    def test_reads_and_parses(self, input_dir: Path) -> None:
        data = _minimal_input()
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()

        assert isinstance(result, ContainerInput)
        assert result.group_folder == "test-group"
        assert result.chat_jid == "test@chat.jid"
        assert result.is_admin is False
        assert len(result.messages) == 1
        assert result.messages[0]["content"] == "hello"

    def test_deletes_file_after_read(self, input_dir: Path) -> None:
        (input_dir / "initial.json").write_text(json.dumps(_minimal_input()))

        read_initial_input()

        assert not (input_dir / "initial.json").exists()

    def test_raises_file_not_found_when_missing(self, input_dir: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_initial_input()

    def test_raises_on_invalid_json(self, input_dir: Path) -> None:
        (input_dir / "initial.json").write_text("not valid json{{{")

        with pytest.raises(json.JSONDecodeError):
            read_initial_input()


class TestContainerInputFields:
    """read_initial_input() correctly parses various ContainerInput fields."""

    def test_session_id(self, input_dir: Path) -> None:
        data = _minimal_input(session_id="sess-abc-123")
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.session_id == "sess-abc-123"

    def test_is_admin_true(self, input_dir: Path) -> None:
        data = _minimal_input(is_admin=True)
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.is_admin is True

    def test_is_scheduled_task(self, input_dir: Path) -> None:
        data = _minimal_input(is_scheduled_task=True)
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.is_scheduled_task is True

    def test_system_notices(self, input_dir: Path) -> None:
        notices = ["worktree is dirty", "3 unpushed commits"]
        data = _minimal_input(system_notices=notices)
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.system_notices == notices

    def test_repo_access(self, input_dir: Path) -> None:
        data = _minimal_input(repo_access="readonly")
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.repo_access == "readonly"

    def test_repo_access_empty_string_normalizes_to_none(self, input_dir: Path) -> None:
        data = _minimal_input(repo_access="")
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.repo_access is None

    def test_system_prompt_append(self, input_dir: Path) -> None:
        data = _minimal_input(system_prompt_append="Be helpful.")
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.system_prompt_append == "Be helpful."

    def test_mcp_direct_servers(self, input_dir: Path) -> None:
        servers = [{"name": "tools", "url": "http://localhost:8080", "transport": "sse"}]
        data = _minimal_input(mcp_direct_servers=servers)
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.mcp_direct_servers == servers

    def test_agent_core_config(self, input_dir: Path) -> None:
        core_config = {"model": "claude-sonnet-4-20250514", "temperature": 0.7}
        data = _minimal_input(agent_core_config=core_config)
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.agent_core_config == core_config

    def test_defaults_when_optional_fields_omitted(self, input_dir: Path) -> None:
        """Verify defaults for all optional fields when only required fields are provided."""
        data = _minimal_input()
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.session_id is None
        assert result.is_scheduled_task is False
        assert result.system_notices is None
        assert result.repo_access is None
        assert result.agent_core_module == "agent_runner.cores.claude"
        assert result.agent_core_class == "ClaudeAgentCore"
        assert result.agent_core_config is None
        assert result.system_prompt_append is None
        assert result.mcp_gateway_url is None
        assert result.mcp_gateway_key is None
        assert result.mcp_direct_servers is None

    def test_ignores_unknown_keys(self, input_dir: Path) -> None:
        """Unknown keys in JSON are silently ignored (from_dict contract)."""
        data = _minimal_input(unknown_field="should be ignored", another="also ignored")
        (input_dir / "initial.json").write_text(json.dumps(data))

        result = read_initial_input()
        assert result.group_folder == "test-group"
        assert not hasattr(result, "unknown_field")
