"""Tests for Codex CLI's public process and stream-event contracts."""

from __future__ import annotations

import asyncio
import json
import tomllib
from unittest.mock import AsyncMock, patch

import pytest

from agent_runner.core import AgentCoreConfig
from agent_runner.cores.codex import CodexCLIAgentCore
from agent_runner.events import ResultEvent, SystemEvent


def _core(
    session_id: str | None = None,
    *,
    extra: dict[str, object] | None = None,
) -> CodexCLIAgentCore:
    return CodexCLIAgentCore(
        AgentCoreConfig(
            cwd="/home/agent/src/owner/project",
            session_id=session_id,
            group_folder="g",
            chat_jid="j",
            is_admin=False,
            is_scheduled_task=False,
            system_prompt_append="Follow the local Pynchy prompts.",
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
            extra=extra or {"model": "gpt-5.2-codex"},
        )
    )


def _set_gateway_env(
    monkeypatch: pytest.MonkeyPatch, base_url: str = "http://gateway:4000"
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", base_url)
    monkeypatch.setenv("OPENAI_API_KEY", "gw-test")


class _FakeStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, content: bytes) -> None:
        self.writes.append(content)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class _FinishedStdout:
    def __init__(self, lines: list[bytes] | None = None) -> None:
        self._lines = iter(lines or [])

    def __aiter__(self) -> _FinishedStdout:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeStderr:
    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(
        self,
        *,
        returncode: int | None = 0,
        stdout: _FinishedStdout | None = None,
    ) -> None:
        self.returncode = returncode
        self.stdin = _FakeStdin()
        self.stdout = stdout or _FinishedStdout()
        self.stderr = _FakeStderr()

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""

    async def wait(self) -> int:
        return self.returncode or 0


async def _run_query(
    core: CodexCLIAgentCore,
    proc: _FakeProc,
) -> tuple[list[object], AsyncMock]:
    with patch(
        "agent_runner.cores.cli_process.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=proc,
    ) as spawn:
        events = [event async for event in core.query("hello")]
    return events, spawn


def _run_public_query(core: CodexCLIAgentCore, proc: _FakeProc) -> tuple[list[object], AsyncMock]:
    return asyncio.run(_run_query(core, proc))


def _started_core(tmp_path, monkeypatch, **kwargs) -> CodexCLIAgentCore:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    core = _core(**kwargs)
    asyncio.run(core.start())
    return core


def _command_args(spawn: AsyncMock) -> tuple[object, ...]:
    return spawn.await_args.args


def _json_line(event: dict[str, object]) -> bytes:
    return (json.dumps(event) + "\n").encode()


def test_start_writes_codex_config_with_hooks_and_mcp(tmp_path, monkeypatch):
    core = _started_core(tmp_path, monkeypatch)

    config = tomllib.loads((tmp_path / "config.toml").read_text())
    expected_top_level = {
        "model": "gpt-5.2-codex",
        "model_provider": "pynchy_litellm",
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
    }
    for key, expected_value in expected_top_level.items():
        assert config[key] == expected_value

    assert config["skills"]["config"] == [
        {
            "path": str(tmp_path / "skills" / ".system" / skill_name / "SKILL.md"),
            "enabled": False,
        }
        for skill_name in ("plugin-creator", "skill-creator", "skill-installer")
    ]
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
    assert core.session_id is None


def test_start_preserves_native_codex_plugin_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch)
    (tmp_path / "config.toml").write_text(
        "[marketplaces.obsidian-knowledge]\n"
        'source = "/opt/plugins/obsidian-knowledge"\n\n'
        '[plugins."obsidian-knowledge@obsidian-knowledge"]\n'
        "enabled = true\n\n"
        "[unrelated]\n"
        "enabled = true\n"
    )

    asyncio.run(_core().start())

    config = tomllib.loads((tmp_path / "config.toml").read_text())
    assert config["marketplaces"]["obsidian-knowledge"]["source"] == (
        "/opt/plugins/obsidian-knowledge"
    )
    assert config["plugins"]["obsidian-knowledge@obsidian-knowledge"]["enabled"] is True
    assert "unrelated" not in config


def test_start_writes_configured_model_reasoning_effort(tmp_path, monkeypatch):
    core = _started_core(
        tmp_path,
        monkeypatch,
        extra={"model": "gpt-5.6-terra", "model_reasoning_effort": "ultra"},
    )

    config = tomllib.loads((tmp_path / "config.toml").read_text())
    assert config["model"] == "gpt-5.6-terra"
    assert config["model_reasoning_effort"] == "ultra"
    assert core.session_id is None


def test_start_can_disable_pynchy_hooks_for_host_direct_mode(tmp_path, monkeypatch):
    _started_core(
        tmp_path,
        monkeypatch,
        extra={"model": "gpt-5.2-codex", "pynchy_hooks_enabled": False},
    )

    config = tomllib.loads((tmp_path / "config.toml").read_text())
    assert "hooks" not in config
    assert "features" not in config


def test_start_rejects_missing_gateway_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "gw-test")

    with pytest.raises(RuntimeError, match="OPENAI_BASE_URL"):
        asyncio.run(_core().start())


def test_start_normalizes_gateway_base_url_with_v1_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    _set_gateway_env(monkeypatch, "http://gateway:4000/v1/")
    asyncio.run(_core().start())

    config = tomllib.loads((tmp_path / "config.toml").read_text())
    assert config["model_providers"]["pynchy_litellm"]["base_url"] == "http://gateway:4000/v1"


def test_query_uses_fallback_installer_path_when_codex_not_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_runner.cores.codex.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "agent_runner.cores.codex.Path.exists",
        lambda self: str(self) == "/usr/local/bin/codex",
    )
    core = _started_core(tmp_path, monkeypatch)

    _events, spawn = _run_public_query(core, _FakeProc())

    assert _command_args(spawn)[0] == "/usr/local/bin/codex"


def test_new_session_query_spawns_expected_codex_command(tmp_path, monkeypatch):
    core = _started_core(tmp_path, monkeypatch)

    _events, spawn = _run_public_query(core, _FakeProc())

    args = _command_args(spawn)
    assert str(args[0]).endswith("codex")
    assert args[1] == "--cd"
    assert "/home/agent/src/owner/project" in args
    assert "--ask-for-approval" in args
    assert "never" in args
    assert "--sandbox" in args
    assert "danger-full-access" in args
    assert "--dangerously-bypass-hook-trust" in args
    assert args[-6:] == ("exec", "--json", "--skip-git-repo-check", "--model", "gpt-5.2-codex", "-")


def test_query_skips_missing_group_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_runner.cores.codex.Path.exists", lambda self: False)
    core = _started_core(tmp_path, monkeypatch)

    _events, spawn = _run_public_query(core, _FakeProc())

    assert "/home/agent/workspace" not in _command_args(spawn)


def test_query_adds_mounted_group_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_runner.cores.codex.Path.exists", lambda self: True)
    core = _started_core(tmp_path, monkeypatch)

    _events, spawn = _run_public_query(core, _FakeProc())

    args = _command_args(spawn)
    add_dir = args.index("--add-dir")
    assert args[add_dir : add_dir + 2] == ("--add-dir", "/home/agent/workspace")


@pytest.mark.parametrize(
    ("session_id", "expected_suffix"),
    [
        ("codex:thread-019c6e27", ("thread-019c6e27", "-")),
        ("codex:gpt-5.2-codex:thread-019c6e27", ("thread-019c6e27", "-")),
    ],
)
def test_resumed_session_query_passes_original_thread_id(
    tmp_path, monkeypatch, session_id, expected_suffix
):
    core = _started_core(tmp_path, monkeypatch, session_id=session_id)

    _events, spawn = _run_public_query(core, _FakeProc())

    args = _command_args(spawn)
    assert "resume" in args
    assert args[-2:] == expected_suffix


def test_query_ignores_foreign_session_id(tmp_path, monkeypatch):
    core = _started_core(tmp_path, monkeypatch, session_id="019c6e27-e55b-73d1-87d8-4e01f1f75043")

    _events, spawn = _run_public_query(core, _FakeProc())

    assert "resume" not in _command_args(spawn)
    assert core.session_id is None


def test_query_sends_system_prompt_to_codex_stdin(tmp_path, monkeypatch):
    core = _started_core(tmp_path, monkeypatch)
    proc = _FakeProc()

    _events, _spawn = _run_public_query(core, proc)

    expected = f"{core.config.system_prompt_append}\n\nUser message:\nhello\n".encode()
    assert proc.stdin.writes == [expected]


def test_resumed_query_does_not_repeat_system_prompt_in_user_input(tmp_path, monkeypatch):
    core = _started_core(tmp_path, monkeypatch, session_id="codex:thread-019c6e27")
    proc = _FakeProc()

    _events, _spawn = _run_public_query(core, proc)

    assert proc.stdin.writes == [b"hello\n"]


def test_stream_event_maps_thread_started_and_exposes_session_id():
    core = _core()

    events = core.map_stream_event({"type": "thread.started", "thread_id": "thread-1"})

    assert [event.type for event in events] == ["system"]
    assert core.session_id == "codex:gpt-5.2-codex:thread-1"
    assert isinstance(events[0], SystemEvent)
    assert events[0].system_data["session_id"] == "codex:gpt-5.2-codex:thread-1"


def test_stream_event_maps_terminal_failure_to_error_result():
    (event,) = _core().map_stream_event(
        {"type": "turn.failed", "error": {"message": "auth failed", "code": "not_logged_in"}}
    )

    assert isinstance(event, ResultEvent)
    assert event.result == "auth failed"
    assert event.result_metadata.is_error is True
    assert event.result_metadata.subtype == "not_logged_in"


def test_public_queries_reset_terminal_error_guard_between_turns():
    core = _core()
    first_proc = _FakeProc(
        returncode=1,
        stdout=_FinishedStdout(
            [
                _json_line({"type": "turn.failed", "error": {"message": "first failed"}}),
                _json_line({"type": "error", "error": {"message": "first failed"}}),
            ]
        ),
    )
    second_proc = _FakeProc(
        returncode=1,
        stdout=_FinishedStdout(
            [
                _json_line({"type": "error", "error": {"message": "second failed"}}),
                _json_line({"type": "turn.failed", "error": {"message": "second failed"}}),
            ]
        ),
    )

    async def run_queries():
        with patch(
            "agent_runner.cores.cli_process.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=[first_proc, second_proc],
        ):
            first = [event async for event in core.query("first")]
            second = [event async for event in core.query("second")]
        return first, second

    first, second = asyncio.run(run_queries())

    assert [event.result for event in first if isinstance(event, ResultEvent)] == ["first failed"]
    assert [event.result for event in second if isinstance(event, ResultEvent)] == ["second failed"]
