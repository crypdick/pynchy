"""Public behavior of the agent-runner host-interaction tools."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from mcp.types import TextContent

sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src"),
)

from agent_runner.agent_tools import AgentToolRuntime, call_tool, use_agent_tool_runtime


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
