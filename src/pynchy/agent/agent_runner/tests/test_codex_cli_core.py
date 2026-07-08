"""Tests for the Codex CLI agent core.

The core drives ``codex exec --json`` and maps Codex JSONL events back into
Pynchy's provider-agnostic ``AgentEvent`` stream.
"""

from __future__ import annotations

import asyncio
import signal
import tomllib

import pytest

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.codex import CodexCLIAgentCore


def _core(session_id: str | None = None) -> CodexCLIAgentCore:
    return CodexCLIAgentCore(
        AgentCoreConfig(
            cwd="/workspace/project",
            session_id=session_id,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
            system_prompt_append="Follow the local Pynchy directives.",
            mcp_servers={
                "pynchy": {
                    "command": "python",
                    "args": ["-m", "agent_runner.agent_tools"],
                    "env": {"PYNCHY_CHAT_JID": "j"},
                },
                "browser": {"type": "http", "url": "http://browser:3000/mcp"},
                "remote-auth": {
                    "url": "https://example.test/mcp",
                    "headers": {"X-Static": "yes"},
                    "auth_value_env": "REMOTE_TOKEN",
                },
            },
            extra={"model": "gpt-5.2-codex"},
        )
    )


def _set_gateway_env(
    monkeypatch: pytest.MonkeyPatch, base_url: str = "http://gateway:4000"
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "gw-test")


def test_start_writes_codex_config_with_hooks_and_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    core = _core()

    asyncio.run(core.start())

    config = tomllib.loads((tmp_path / "config.toml").read_text())
    expected_top_level = {
        "model_provider": "pynchy_litellm",
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
    }
    for key, expected_value in expected_top_level.items():
        assert config[key] == expected_value

    assert config["model_providers"]["pynchy_litellm"] == {
        "name": "Pynchy LiteLLM Gateway",
        "base_url": "http://gateway:4000/v1",
        "wire_api": "responses",
        "env_key": "OPENAI_API_KEY",
    }
    assert config["features"]["hooks"] is True

    expected_mcp_servers = {
        "pynchy": {
            "command": "python",
            "args": ["-m", "agent_runner.agent_tools"],
            "env": {"PYNCHY_CHAT_JID": "j"},
        },
        "browser": {"url": "http://browser:3000/mcp"},
        "remote-auth": {
            "bearer_token_env_var": "REMOTE_TOKEN",
            "http_headers": {"X-Static": "yes"},
        },
    }
    for server_name, expected_fields in expected_mcp_servers.items():
        for field_name, expected_value in expected_fields.items():
            assert config["mcp_servers"][server_name][field_name] == expected_value

    hooks = config["hooks"]["PreToolUse"]
    assert hooks[0]["matcher"] == "*"
    assert hooks[0]["hooks"][0]["command"].endswith("-m agent_runner.security.hook_entry")


def test_start_rejects_missing_gateway_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "gw-test")
    core = _core()

    with pytest.raises(RuntimeError, match="OPENAI_BASE_URL"):
        asyncio.run(core.start())


def test_start_normalizes_gateway_base_url_with_v1_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch, "http://gateway:4000/v1/")
    core = _core()

    asyncio.run(core.start())

    config = tomllib.loads((tmp_path / "config.toml").read_text())
    assert config["model_providers"]["pynchy_litellm"]["base_url"] == "http://gateway:4000/v1"


def test_start_falls_back_to_installer_path_when_codex_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    monkeypatch.setattr("agent_runner.cores.codex.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "agent_runner.cores.codex.Path.exists",
        lambda self: str(self) == "/usr/local/bin/codex",
    )
    core = _core()

    asyncio.run(core.start())

    assert core._codex_path == "/usr/local/bin/codex"


def test_build_args_for_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    core = _core()
    asyncio.run(core.start())

    args = core._build_args()

    assert args[:2] == [core._codex_path, "--cd"]
    assert "/workspace/project" in args
    assert "--ask-for-approval" in args
    assert "never" in args
    assert "--sandbox" in args
    assert "danger-full-access" in args
    assert "--dangerously-bypass-hook-trust" in args
    assert args[-6:] == ["exec", "--json", "--skip-git-repo-check", "--model", "gpt-5.2-codex", "-"]


def test_build_args_skips_missing_group_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    monkeypatch.setattr("agent_runner.cores.codex.Path.exists", lambda self: False)
    core = _core()
    asyncio.run(core.start())

    args = core._build_args()

    assert "/workspace/group" not in args


def test_build_args_adds_group_workspace_when_mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    monkeypatch.setattr("agent_runner.cores.codex.Path.exists", lambda self: True)
    core = _core()
    asyncio.run(core.start())

    args = core._build_args()

    assert args[args.index("--add-dir") : args.index("--add-dir") + 2] == [
        "--add-dir",
        "/workspace/group",
    ]


def test_build_args_for_resumed_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    core = _core(session_id="codex:thread-019c6e27")
    asyncio.run(core.start())

    args = core._build_args()

    assert "resume" in args
    assert args[-7:] == [
        "resume",
        "--json",
        "--skip-git-repo-check",
        "--model",
        "gpt-5.2-codex",
        "thread-019c6e27",
        "-",
    ]


def test_build_args_ignores_foreign_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    core = _core(session_id="019c6e27-e55b-73d1-87d8-4e01f1f75043")
    asyncio.run(core.start())

    args = core._build_args()

    assert "resume" not in args
    assert core.session_id is None
    assert args[-6:] == ["exec", "--json", "--skip-git-repo-check", "--model", "gpt-5.2-codex", "-"]


def test_build_stdin_includes_system_prompt():
    assert _core()._build_stdin("hello").decode() == (
        "Follow the local Pynchy directives.\n\nUser message:\nhello\n"
    )


def test_thread_started_captures_session_id():
    core = _core()

    events = core._map_event({"type": "thread.started", "thread_id": "thread-1"})

    assert [e.type for e in events] == ["system"]
    assert core.session_id == "codex:thread-1"
    assert events[0].data["system_data"]["session_id"] == "codex:thread-1"


def test_agent_message_maps_to_text_and_last_result():
    core = _core()

    events = core._map_event(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
    )

    assert [e.type for e in events] == ["text"]
    assert events[0].data["text"] == "done"
    assert core._last_agent_message == "done"


def test_command_item_maps_to_tool_events():
    core = _core()

    started = core._map_event(
        {
            "type": "item.started",
            "item": {"id": "cmd-1", "type": "command_execution", "command": "ls -la"},
        }
    )
    completed = core._map_event(
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "ls -la",
                "output": "ok",
            },
        }
    )

    assert started[0].type == "tool_use"
    assert started[0].data == {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
    assert completed[0].type == "tool_result"
    assert completed[0].data["tool_result_id"] == "cmd-1"
    assert completed[0].data["tool_result_content"] == "ok"


def test_turn_failed_maps_to_error_result():
    core = _core()

    (event,) = core._map_event(
        {"type": "turn.failed", "error": {"message": "auth failed", "code": "not_logged_in"}}
    )

    assert event.type == "result"
    assert event.data["result"] == "auth failed"
    assert event.data["result_metadata"]["is_error"] is True
    assert event.data["result_metadata"]["subtype"] == "not_logged_in"


class _FakeProc:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.signals: list[int] = []
        self.killed = False

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True

    async def wait(self):
        return 0


def test_stop_sends_sigint_and_clears_proc():
    core = _core()
    proc = _FakeProc(returncode=None)
    core._proc = proc
    asyncio.run(core.stop())

    assert proc.signals == [signal.SIGINT]
    assert proc.killed is False
    assert core._proc is None
