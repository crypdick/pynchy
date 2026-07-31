"""Public direct-host runner behavior at its stdin/stdout boundary."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)

from agent_runner import host_direct
from agent_runner.events import ResultEvent, ResultMetadata, SystemEvent, TextEvent
from agent_runner.models import ContainerInput


def _input(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "messages": [],
        "group_folder": "group",
        "chat_jid": "chat",
        "is_admin": True,
        "query_id": "query-1",
        "agent_core_module": "agent_runner.cores.openai",
        "agent_core_class": "OpenAIAgentCore",
    }
    value.update(overrides)
    return value


def _envelope(**overrides: object) -> str:
    return json.dumps({"input": _input(**overrides), "cwd": "/workspace"})


def test_host_core_config_ignores_unnamed_direct_servers_and_rewrites_urls() -> None:
    config = host_direct.build_host_core_config(
        ContainerInput.from_dict(
            _input(
                mcp_direct_servers=[
                    {"url": "http://ignored:8000", "transport": "sse"},
                    {"name": "docs", "url": "http://proxy:8000/path", "transport": "sse"},
                ]
            )
        ),
        cwd="/workspace",
    )

    assert "ignored" not in config.mcp_servers
    assert config.mcp_servers["docs"] == {
        "type": "sse",
        "url": "http://localhost:8000/path/sse",
    }


@pytest.mark.parametrize(
    ("server", "error"),
    [
        ({"name": "docs", "url": 42}, "direct MCP server URL must be a string"),
        (
            {"name": "docs", "url": "/relative/path"},
            "direct MCP server URL must include a hostname",
        ),
    ],
)
def test_host_core_config_rejects_invalid_direct_server_urls(
    server: dict[str, object], error: str
) -> None:
    with pytest.raises((TypeError, ValueError), match=error):
        host_direct.build_host_core_config(
            ContainerInput.from_dict(_input(mcp_direct_servers=[server])), cwd="/workspace"
        )


def test_host_runner_streams_query_events_and_query_id(monkeypatch, capsys) -> None:
    class _Core:
        session_id = None

        async def start(self) -> None:
            self.session_id = "session-2"

        async def query(self, _prompt: str):
            yield SystemEvent("init", {"session_id": "session-from-event"})
            yield TextEvent("hello")
            yield ResultEvent("done", ResultMetadata("completed", False, "session-2"))

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(host_direct, "create_agent_core", lambda *_args: _Core())
    monkeypatch.setattr(host_direct.sys, "stdin", io.StringIO(_envelope()))

    with pytest.raises(SystemExit) as exit_info:
        host_direct.main()
    assert exit_info.value.code == 0

    outputs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [output["type"] for output in outputs] == ["system", "text", "result"]
    assert all(output["query_id"] == "query-1" for output in outputs)
    assert outputs[-1]["new_session_id"] == "session-2"


def test_host_runner_reports_startup_and_input_failures(monkeypatch, capsys) -> None:
    monkeypatch.setattr(host_direct.sys, "stdin", io.StringIO("[]"))
    with pytest.raises(SystemExit) as exit_info:
        host_direct.main()
    assert exit_info.value.code == 1
    startup_output = json.loads(capsys.readouterr().out)
    assert startup_output["status"] == "error"
    assert "host runner input must be a JSON object" in startup_output["error"]

    monkeypatch.setattr(host_direct.sys, "stdin", io.StringIO(_envelope()))
    monkeypatch.setattr(
        host_direct,
        "create_agent_core",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("core unavailable")),
    )
    with pytest.raises(SystemExit) as exit_info:
        host_direct.main()
    assert exit_info.value.code == 1
    core_output = json.loads(capsys.readouterr().out)
    assert core_output["query_id"] == "query-1"
    assert core_output["error"] == "Failed to start host runner: core unavailable"


def test_host_runner_reports_agent_failure_and_cleanup_failure(monkeypatch, capsys) -> None:
    class _Core:
        session_id = "session-1"

        async def start(self) -> None:
            return None

        def query(self, _prompt: str):
            raise RuntimeError("query failed")

        async def stop(self) -> None:
            raise RuntimeError("stop failed")

    monkeypatch.setattr(host_direct, "create_agent_core", lambda *_args: _Core())
    monkeypatch.setattr(host_direct.sys, "stdin", io.StringIO(_envelope(session_id="session-1")))

    with pytest.raises(SystemExit) as exit_info:
        host_direct.main()
    assert exit_info.value.code == 1

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["error"] == "query failed"
    assert output["new_session_id"] == "session-1"
    assert "stop failed" in captured.err
