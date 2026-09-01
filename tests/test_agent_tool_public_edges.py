"""Public behavior of the agent-runner host-interaction tools."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from subprocess import CalledProcessError  # noqa: S404 - test-only failure injection.
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


def _runtime(tmp_path: Path, *, turn_id: str = "") -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="slack:group",
        group_folder="group",
        is_admin=False,
        is_scheduled_task=False,
        ipc_dir=tmp_path,
        ask_user_timeout_seconds=17.0,
        turn_id=turn_id,
    )


def _admin_runtime(tmp_path: Path) -> AgentToolRuntime:
    return AgentToolRuntime(
        chat_jid="slack:admin",
        group_folder="admin",
        is_admin=True,
        is_scheduled_task=False,
        ipc_dir=tmp_path,
    )


def _publication_response(tmp_path: Path, result: dict[str, object]) -> None:
    response_file = tmp_path / "merge_results" / "1700000000000-fixed.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(json.dumps(result), encoding="utf-8")


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


async def test_admin_deploy_reports_an_unavailable_git_revision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_request = Mock()
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_admin._ipc.write_request_file", write_request
    )
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_admin.subprocess.run",
        Mock(side_effect=CalledProcessError(1, ["git", "rev-parse", "HEAD"])),
    )

    with use_agent_tool_runtime(_admin_runtime(tmp_path)):
        result = await call_tool("deploy_changes", {})

    assert result[0].text.startswith("Deploy initiated (HEAD: ).")
    assert not write_request.call_args.args[1]["headSha"]


async def test_admin_deploy_continues_when_source_mount_is_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_request = Mock()
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_admin._ipc.write_request_file", write_request
    )
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_admin.subprocess.run",
        Mock(side_effect=FileNotFoundError("source mount is unavailable")),
    )

    with use_agent_tool_runtime(_admin_runtime(tmp_path)):
        result = await call_tool("deploy_changes", {})

    assert result[0].text.startswith("Deploy initiated (HEAD: ).")
    assert not write_request.call_args.args[1]["headSha"]


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


async def test_host_service_request_uses_polling_when_watchdog_misses_the_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response_file = tmp_path / "responses" / "request-4.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(json.dumps({"result": {"ready": True}}), encoding="utf-8")
    observer = _Observer()
    exists = Mock(side_effect=(False, False, False, True))
    wait_calls: list[float] = []

    async def wait_for(awaitable: object, **kwargs: float) -> bool:
        await asyncio.sleep(0)
        wait_calls.append(kwargs["timeout"])
        awaitable.close()
        return True

    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.Observer", lambda: observer)
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request._response_file_exists", exists)
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.asyncio.wait_for", wait_for)
    monkeypatch.setattr("agent_runner.agent_tools._ipc.write_request_file", Mock())

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await request_host_service("calendar", {}, guarded_action_id="request-4")

    assert result[0].text == json.dumps({"ready": True}, indent=2)
    assert len(wait_calls) == 1


async def test_host_service_request_falls_back_to_polling_when_watchdog_cannot_start(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response_file = tmp_path / "responses" / "request-5.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(json.dumps({"result": {"ready": True}}), encoding="utf-8")

    class _UnavailableObserver(_Observer):
        def start(self) -> None:
            raise OSError("watch limit")

    observer = _UnavailableObserver()
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.Observer", lambda: observer)

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await request_host_service("calendar", {}, guarded_action_id="request-5")

    assert result[0].text == json.dumps({"ready": True}, indent=2)
    assert observer.stopped is False


async def test_host_service_request_continues_after_a_poll_cycle_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response_file = tmp_path / "responses" / "request-6.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text(json.dumps({"result": {"ready": True}}), encoding="utf-8")
    observer = _Observer()
    exists = Mock(side_effect=(False, False, False, True))
    original_sleep = asyncio.sleep

    async def wait_for(awaitable: object, **_kwargs: float) -> bool:
        await original_sleep(0)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.Observer", lambda: observer)
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request._response_file_exists", exists)
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.asyncio.wait_for", wait_for)
    monkeypatch.setattr("agent_runner.agent_tools._ipc.write_request_file", Mock())

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await request_host_service("calendar", {}, guarded_action_id="request-6")

    assert result[0].text == json.dumps({"ready": True}, indent=2)


async def test_host_service_request_reports_an_immediate_timeout(
    monkeypatch,
    tmp_path: Path,
) -> None:
    observer = _Observer()
    monkeypatch.setattr("agent_runner.agent_tools._ipc_request.Observer", lambda: observer)
    monkeypatch.setattr("agent_runner.agent_tools._ipc.write_request_file", Mock())

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await request_host_service(
            "calendar",
            {},
            response_timeout_seconds=0,
            guarded_action_id="request-7",
        )

    assert result[0].text == "Error: Request timed out waiting for host response"


async def test_sync_worktree_tool_reports_a_successful_pull_request_publication(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _publication_response(tmp_path, {"success": True, "message": "https://github/pr/1"})
    write_request = Mock()
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle.time.time", lambda: 1700000000.0)
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.secrets.token_hex",
        lambda _: "fixed",
    )
    monkeypatch.setattr("agent_runner.agent_tools._ipc.write_request_file", write_request)

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool(
            "sync_worktree_to_main", {"title": "Test publication", "body": "Test body"}
        )

    assert result[0].text == "https://github/pr/1"
    write_request.assert_called_once_with(
        "sync_worktree_to_main",
        {
            "groupFolder": "group",
            "publication": "pull-request",
            "title": "Test publication",
            "body": "Test body",
        },
        request_id="1700000000000-fixed",
        reply_to="merge_results",
    )


async def test_sync_worktree_tool_includes_the_active_turn_id(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _publication_response(tmp_path, {"success": True, "message": "https://github/pr/2"})
    write_request = Mock()
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle.time.time", lambda: 1700000000.0)
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.secrets.token_hex",
        lambda _: "fixed",
    )
    monkeypatch.setattr("agent_runner.agent_tools._ipc.write_request_file", write_request)

    with use_agent_tool_runtime(_runtime(tmp_path, turn_id="turn-1")):
        result = await call_tool(
            "sync_worktree_to_main", {"title": "Test publication", "body": "Test body"}
        )

    assert result[0].text == "https://github/pr/2"
    assert write_request.call_args.kwargs["request_id"] == "1700000000000-fixed"
    assert write_request.call_args.args[1]["turn_id"] == "turn-1"


async def test_sync_worktree_tool_reports_per_repository_publication_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _publication_response(
        tmp_path,
        {
            "success": False,
            "message": "aggregate failure",
            "repos": {"pynchy": {"message": "branch is dirty"}},
        },
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle.time.time", lambda: 1700000000.0)
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.secrets.token_hex",
        lambda _: "fixed",
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool(
            "sync_worktree_to_main", {"title": "Test publication", "body": "Test body"}
        )

    assert result.isError is True
    assert result.content[0].text == "pynchy: branch is dirty"


async def test_publication_tool_recovers_from_a_malformed_host_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    response_file = tmp_path / "merge_results" / "1700000000000-fixed.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)
    response_file.write_text("not json", encoding="utf-8")
    write_request = Mock()
    original_sleep = asyncio.sleep

    async def repair_response(_delay: float) -> None:
        await original_sleep(0)
        response_file.write_text(
            json.dumps({"success": True, "message": "recovered"}),
            encoding="utf-8",
        )

    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle.time.time", lambda: 1700000000.0)
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.secrets.token_hex",
        lambda _: "fixed",
    )
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle._ipc.write_request_file",
        write_request,
    )
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.asyncio.sleep",
        repair_response,
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool(
            "sync_worktree_to_main", {"title": "Test publication", "body": "Test body"}
        )

    assert result[0].text == "recovered"


async def test_publication_tool_reports_timeout_without_a_host_response(
    monkeypatch,
    tmp_path: Path,
) -> None:
    clock = iter((1700000000.0, 1700000000.0, 1700000001.0, 1700000121.0))
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle.time.time", lambda: next(clock))
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.secrets.token_hex",
        lambda _: "fixed",
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle._ipc.write_request_file", Mock())
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.asyncio.sleep",
        AsyncMock(),
    )

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool(
            "sync_worktree_to_main", {"title": "Test publication", "body": "Test body"}
        )

    assert result.isError is True
    assert result.content[0].text == "Timed out (120s). Retry or check with the host."


async def test_managed_feature_publication_forwards_the_feature_slug(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _publication_response(tmp_path, {"success": True, "message": "https://github/pr/3"})
    write_request = Mock()
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle.time.time", lambda: 1700000000.0)
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle.secrets.token_hex",
        lambda _: "fixed",
    )
    monkeypatch.setattr("agent_runner.agent_tools._ipc.write_request_file", write_request)

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("publish_managed_feature", {"feature_slug": "feature-1"})

    assert result[0].text == "https://github/pr/3"
    assert write_request.call_args.args[:2] == (
        "publish_managed_feature",
        {"feature_slug": "feature-1", "publication": "pull-request"},
    )


async def test_reset_context_writes_a_handoff_request_before_exit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_request = Mock()
    exit_container = Mock()
    write_request_path = "agent_runner.agent_tools._tools_lifecycle._ipc.write_request_file"
    monkeypatch.setattr(
        write_request_path,
        write_request,
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle._exit_container", exit_container)

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("reset_context", {"message": "continue from here"})

    assert result is None
    write_request.assert_called_once_with(
        "reset_context",
        {"chatJid": "slack:group", "groupFolder": "group", "message": "continue from here"},
        reply_to=None,
    )
    exit_container.assert_called_once_with()


async def test_reset_context_writes_the_close_sentinel_without_a_message(
    monkeypatch,
    tmp_path: Path,
) -> None:
    write_request = Mock()
    exit_container = Mock()
    monkeypatch.setattr(
        "agent_runner.agent_tools._tools_lifecycle._ipc.write_request_file",
        write_request,
    )
    monkeypatch.setattr("agent_runner.agent_tools._tools_lifecycle.os._exit", exit_container)

    with use_agent_tool_runtime(_runtime(tmp_path)):
        result = await call_tool("reset_context", {})

    assert result is None
    assert (tmp_path / "input" / "_close").exists()
    write_request.assert_called_once_with(
        "reset_context",
        {"chatJid": "slack:group", "groupFolder": "group"},
        reply_to=None,
    )
    exit_container.assert_called_once_with(0)
