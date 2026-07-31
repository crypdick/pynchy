"""Public behavior of the agent-runner host-interaction tools."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from mcp.types import TextContent

sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"),
)

from agent_runner.agent_tools import (
    AgentToolRuntime,
    call_tool,
    request_host_service,
    use_agent_tool_runtime,
)


def _runtime(tmp_path: Path) -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="slack:group",
        group_folder="group",
        is_admin=False,
        is_scheduled_task=False,
        ipc_dir=tmp_path,
        ask_user_timeout_seconds=17.0,
    )


def _admin_runtime(tmp_path: Path) -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="slack:admin",
        group_folder="admin",
        is_admin=True,
        is_scheduled_task=False,
        ipc_dir=tmp_path,
    )


class _Observer:
    def __init__(self) -> None:
        self.daemon = False
        self.started = False
        self.stopped = False

    def schedule(self, *_args: object, **_kwargs: object) -> None:
        return None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def join(self, **_kwargs: object) -> None:
        return None


async def test_ask_user_rejects_an_empty_question_list(tmp_path: Path) -> None:
    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("ask_user", {"questions": []})

    assert result.isError is True
    assert result.content[0].text == "questions list must be non-empty"


async def test_ask_user_forwards_questions_with_the_runtime_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response = [TextContent(type="text", text="answer")]
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_ask_user.ipc_service_request",
        request,
    )
    questions = [{"question": "Continue?"}]

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("ask_user", {"questions": questions})

    assert result == response
    request.assert_awaited_once_with(
        "ask_user",
        {"questions": questions},
        response_timeout_seconds=17.0,
        type_override="ask_user:ask",
    )


async def test_register_group_rejects_non_admin_requests(tmp_path: Path) -> None:
    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool(
            "register_group",
            {"jid": "chat", "name": "Chat", "folder": "chat", "trigger": "@Pynchy"},
        )

    assert result.isError is True
    assert result.content[0].text == "Only the admin group can register new groups."


async def test_admin_tools_write_registration_and_deploy_requests(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_request = Mock()
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_admin._ipc.write_request_file", write_request
    )
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_admin.subprocess.run",
        lambda *args, **kwargs: Mock(stdout="abc123\n"),
    )
    monkeypatch.setenv("PYNCHY_SESSION_ID", "session-1")

    with use_agent_tool_runtime(_admin_runtime(tmp_path)):
        register_result = await call_tool(
            "register_group",
            {"jid": "chat", "name": "Chat", "folder": "chat", "trigger": "@Pynchy"},
        )
        deploy_result = await call_tool(
            "deploy_changes",
            {"rebuild_container": True, "resume_prompt": "resume"},
        )

    assert (
        register_result[0].text
        == 'Group "Chat" registered. It will start receiving messages immediately.'
    )
    assert deploy_result[0].text.startswith("Deploy initiated (HEAD: abc123).")
    assert write_request.call_args_list == [
        (
            (
                "register_group",
                {"jid": "chat", "name": "Chat", "folder": "chat", "trigger": "@Pynchy"},
            ),
            {"reply_to": None},
        ),
        (
            (
                "deploy",
                {
                    "rebuildContainer": True,
                    "resumePrompt": "resume",
                    "headSha": "abc123",
                    "sessionId": "session-1",
                    "chatJid": "slack:admin",
                },
            ),
            {"reply_to": None},
        ),
    ]


async def test_host_service_request_reads_an_already_available_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response_file = tmp_path / "responses" / "request-1.json"
    response_file.parent.mkdir()
    response_file.write_text(json.dumps({"result": {"ok": True}}), encoding="utf-8")
    observer = _Observer()
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.Observer", lambda: observer)

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await request_host_service(
            "calendar",
            {"date": "today"},
            guarded_action_id="request-1",
        )

    assert result[0].text == json.dumps({"ok": True}, indent=2)
    assert observer.started is True
    assert observer.stopped is True


async def test_host_service_request_reads_a_response_written_after_request(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response_file = tmp_path / "responses" / "request-2.json"
    observer = _Observer()
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.Observer", lambda: observer)

    def write_request(*_args: object, **_kwargs: object) -> tuple[str, str]:
        response_file.parent.mkdir(parents=True, exist_ok=True)
        response_file.write_text(json.dumps({"error": "denied"}), encoding="utf-8")
        return ("request.json", "request-2")

    monkeypatch.setattr("agent_runner.agent_tools._ipc.write_request_file", write_request)

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await request_host_service("calendar", {}, guarded_action_id="request-2")

    assert result[0].text == "Error: denied"


async def test_host_service_request_reports_a_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observer = _Observer()
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.Observer", lambda: observer)
    monkeypatch.setattr(
        "agent_runner.agent_tools._ipc_request._wait_for_response_file",
        AsyncMock(side_effect=TimeoutError),
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await request_host_service("calendar", {}, guarded_action_id="request-3")

    assert result[0].text == "Error: Request timed out waiting for host response"
