"""OpenAI Codex CLI agent core.

Drives ``codex exec --json`` as a subprocess while routing model traffic through
Pynchy's OpenAI API gateway. This keeps Codex-specific sessions, JSONL events,
and tooling, but leaves provider auth and model routing with LiteLLM.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_runner.core import AgentCoreConfig, AgentEvent

from ._codex_config import (
    DEFAULT_CODEX_SANDBOX_MODE,
    gateway_base_url_from_env,
    write_codex_config,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_STREAM_LINE_LIMIT = 32 * 1024 * 1024
_CODEX_SESSION_PREFIX = "codex:"
_CORE_NAME = "codex-cli"


def _log(message: str) -> None:
    """Log to stderr (captured by the host container runner)."""
    sys.stderr.write(f"[codex-cli-core] {message}\n")
    sys.stderr.flush()


def _require_stream[TStream](stream: TStream | None, stream_name: str) -> TStream:
    if stream is None:
        raise RuntimeError(f"{_CORE_NAME} subprocess missing {stream_name} stream after creation")
    return stream


def _codex_home() -> Path:
    """Resolve the Codex home used by this container process."""
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured)
    return Path.home() / ".codex"


def _resolve_codex_path() -> str:
    """Resolve the Codex binary even when the container PATH is sparse."""
    if path := shutil.which("codex"):
        return path
    installer_path = Path("/usr/local/bin/codex")
    if installer_path.exists():
        return str(installer_path)
    return "codex"


def _codex_thread_id(session_id: str | None) -> str | None:
    """Return the raw Codex thread id for Pynchy-owned Codex sessions."""
    if not session_id or not session_id.startswith(_CODEX_SESSION_PREFIX):
        return None
    thread_id = session_id.removeprefix(_CODEX_SESSION_PREFIX)
    if ":" in thread_id:
        _model, thread_id = thread_id.split(":", maxsplit=1)
    return thread_id or None


def _pynchy_session_id(thread_id: str, model: str | None = None) -> str:
    """Namespace Codex thread ids so other cores never try to resume them."""
    if model:
        return f"{_CODEX_SESSION_PREFIX}{model}:{thread_id}"
    return f"{_CODEX_SESSION_PREFIX}{thread_id}"


def _configured_model(extra: dict[str, Any]) -> str | None:
    model = extra.get("model")
    return str(model) if model else None


def _item_text(item: dict[str, Any]) -> str:
    """Extract display text from common Codex JSONL item shapes."""
    text = item.get("text") or item.get("message") or item.get("summary") or item.get("output")
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "\n".join(
            str(part.get("text", part)) if isinstance(part, dict) else str(part) for part in text
        )
    return ""


def _command_string(value: Any, joiner: str) -> str:
    if isinstance(value, list):
        return joiner.join(str(part) for part in value)
    if value is not None:
        return str(value)
    return ""


def _action_command(action: dict[str, Any]) -> str:
    commands = _command_string(action.get("commands"), " && ")
    if commands:
        return commands
    return _command_string(action.get("command"), " ")


def _item_command(item: dict[str, Any]) -> str:
    command = _command_string(item.get("command") or item.get("cmd"), " ")
    if command:
        return command
    action = item.get("action")
    return _action_command(action) if isinstance(action, dict) else ""


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("call_id") or item.get("callId") or "")


def _item_is_error(item: dict[str, Any]) -> bool:
    return bool(item.get("is_error") or item.get("isError"))


class CodexCLIAgentCore:
    """Agent core that drives ``codex exec --json`` as a subprocess."""

    def __init__(self, config: AgentCoreConfig) -> None:
        self.config = config
        self._session_id: str | None = (
            config.session_id if _codex_thread_id(config.session_id) else None
        )
        self._codex_path: str = "codex"
        self._env: dict[str, str] | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._last_agent_message: str | None = None
        self._last_turn_metadata: dict[str, Any] = {}

    async def start(self) -> None:
        """Resolve the CLI binary, write config, and prepare the environment."""
        self._codex_path = _resolve_codex_path()
        codex_home = _codex_home()
        gateway_base_url = gateway_base_url_from_env()
        write_codex_config(
            codex_home,
            self.config.mcp_servers,
            gateway_base_url=gateway_base_url,
            model=_configured_model(self.config.extra),
        )

        self._env = os.environ.copy()
        self._env["CODEX_HOME"] = str(codex_home)
        self._env["PYNCHY_PLUGIN_HOOKS"] = json.dumps(self.config.plugin_hooks)
        _log(
            f"codex binary: {self._codex_path}; "
            f"CODEX_HOME={codex_home}; gateway={gateway_base_url}; "
            f"MCP servers={list(self.config.mcp_servers)}"
        )

    def _build_args(self) -> list[str]:
        """Assemble ``codex exec`` argv for one turn."""
        sandbox = str(self.config.extra.get("sandbox_mode", DEFAULT_CODEX_SANDBOX_MODE))
        approval = str(self.config.extra.get("approval_policy", "never"))
        thread_id = _codex_thread_id(self._session_id)

        args = [
            self._codex_path,
            "--cd",
            self.config.cwd,
            "--ask-for-approval",
            approval,
            "--sandbox",
            sandbox,
            "--dangerously-bypass-hook-trust",
        ]

        # Real Pynchy containers always create /workspace/group. Host-side wet
        # runs do not, so only add it when the runtime has actually mounted it.
        if self.config.cwd != "/workspace/group" and Path("/workspace/group").exists():
            args += ["--add-dir", "/workspace/group"]

        args.append("exec")
        if thread_id:
            args.append("resume")
        args += ["--json", "--skip-git-repo-check"]

        if model := _configured_model(self.config.extra):
            args += ["--model", model]
        if thread_id:
            args.append(thread_id)
        args.append("-")
        return args

    def _build_stdin(self, prompt: str) -> bytes:
        """Build the raw stdin prompt for ``codex exec -``."""
        if not self.config.system_prompt_append:
            return (prompt + "\n").encode()
        return (f"{self.config.system_prompt_append}\n\nUser message:\n{prompt}\n").encode()

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Spawn one Codex turn and stream mapped events."""
        _log(f"spawn codex (session: {self._session_id or 'new'})")
        proc = await self._spawn_process()
        await self._write_prompt(proc, prompt)

        saw_result = False
        async for event in self._stream_events(proc):
            if event.type == "result":
                saw_result = True
            yield event

        stderr_text, return_code = await self._finish_process(proc)
        if not saw_result:
            yield self._synthesize_result(return_code, stderr_text)

    async def _spawn_process(self) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            *self._build_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.cwd,
            env=self._env,
            limit=_STREAM_LINE_LIMIT,
        )
        self._proc = proc
        return proc

    async def _write_prompt(self, proc: asyncio.subprocess.Process, prompt: str) -> None:
        stdin = _require_stream(proc.stdin, "stdin")
        _ = _require_stream(proc.stdout, "stdout")
        _ = _require_stream(proc.stderr, "stderr")
        stdin.write(self._build_stdin(prompt))
        await stdin.drain()
        stdin.close()

    async def _stream_events(self, proc: asyncio.subprocess.Process) -> AsyncIterator[AgentEvent]:
        stdout = _require_stream(proc.stdout, "stdout")
        async for raw in stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            for event in self._events_from_line(line):
                yield event

    def _events_from_line(self, line: str) -> list[AgentEvent]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            _log(f"skipping non-JSON stdout line: {line[:200]}")
            return []
        return self._map_event(obj) if isinstance(obj, dict) else []

    async def _finish_process(self, proc: asyncio.subprocess.Process) -> tuple[str, int]:
        stderr = _require_stream(proc.stderr, "stderr")
        stderr_text = (await stderr.read()).decode(errors="replace")
        return_code = await proc.wait()
        self._proc = None
        return stderr_text, return_code

    def _synthesize_result(self, return_code: int, stderr_text: str) -> AgentEvent:
        is_error = return_code != 0
        result = self._last_agent_message
        if is_error and not result:
            result = f"codex CLI exited (code {return_code}). {stderr_text[:500]}"
        return AgentEvent(
            type="result",
            data={
                "result": result,
                "result_metadata": {
                    "subtype": "error" if is_error else "success",
                    "is_error": is_error,
                    "session_id": self._session_id,
                    **self._last_turn_metadata,
                },
            },
        )

    def _map_event(self, obj: dict[str, Any]) -> list[AgentEvent]:
        """Map one Codex JSONL event object to zero or more ``AgentEvent``s."""
        event_type = obj.get("type")

        if event_type == "thread.started":
            return self._map_thread_started(obj)

        if event_type in {"turn.completed", "turn.completed_with_errors"}:
            self._record_turn_metadata(obj)
            return []

        if event_type in {"turn.failed", "error"}:
            return [self._map_error_result(obj, str(event_type))]

        if event_type not in {"item.started", "item.completed"}:
            return []

        item = obj.get("item")
        if not isinstance(item, dict):
            return []
        return self._map_item_event(str(event_type), item)

    def _map_thread_started(self, obj: dict[str, Any]) -> list[AgentEvent]:
        sid = obj.get("thread_id") or obj.get("threadId")
        if isinstance(sid, str) and sid:
            self._session_id = _pynchy_session_id(sid, _configured_model(self.config.extra))
        system_data = dict(obj)
        if self._session_id:
            system_data["session_id"] = self._session_id
        return [
            AgentEvent(
                type="system",
                data={"system_subtype": "thread.started", "system_data": system_data},
            )
        ]

    def _record_turn_metadata(self, obj: dict[str, Any]) -> None:
        self._last_turn_metadata = {
            key: value for key, value in obj.items() if key not in {"type", "items", "item"}
        }

    def _map_error_result(self, obj: dict[str, Any], fallback_subtype: str) -> AgentEvent:
        err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
        message = err.get("message") or obj.get("message") or "Codex turn failed"
        subtype = err.get("code") or obj.get("code") or fallback_subtype
        return AgentEvent(
            type="result",
            data={
                "result": str(message),
                "result_metadata": {
                    "subtype": str(subtype),
                    "is_error": True,
                    "session_id": self._session_id,
                },
            },
        )

    def _map_item_event(self, event_type: str, item: dict[str, Any]) -> list[AgentEvent]:
        item_type = item.get("type") or item.get("item_type") or item.get("itemType")

        if item_type == "agent_message" and event_type == "item.completed":
            return self._map_agent_message_item(item)

        if item_type in {"reasoning", "reasoning_item"} and event_type == "item.completed":
            return self._map_reasoning_item(item)

        if item_type in {"command_execution", "command"}:
            return self._map_command_item(event_type, item)

        if item_type in {"mcp_tool_call", "tool_call", "function_call"}:
            return self._map_tool_call_item(event_type, item, str(item_type))

        return []

    def _map_agent_message_item(self, item: dict[str, Any]) -> list[AgentEvent]:
        text = _item_text(item)
        if not text:
            return []
        self._last_agent_message = text
        return [AgentEvent(type="text", data={"text": text})]

    def _map_reasoning_item(self, item: dict[str, Any]) -> list[AgentEvent]:
        text = _item_text(item)
        if not text:
            return []
        return [AgentEvent(type="thinking", data={"thinking": text})]

    def _map_command_item(self, event_type: str, item: dict[str, Any]) -> list[AgentEvent]:
        command = _item_command(item)
        if event_type == "item.started":
            return [
                AgentEvent(
                    type="tool_use",
                    data={"tool_name": "Bash", "tool_input": {"command": command}},
                )
            ]
        return [
            AgentEvent(
                type="tool_result",
                data={
                    "tool_result_id": _item_id(item),
                    "tool_result_content": _item_text(item),
                    "tool_result_is_error": _item_is_error(item),
                },
            )
        ]

    def _map_tool_call_item(
        self, event_type: str, item: dict[str, Any], fallback_name: str
    ) -> list[AgentEvent]:
        name = (
            item.get("tool_name")
            or item.get("toolName")
            or item.get("name")
            or item.get("function_name")
            or fallback_name
        )
        tool_input = self._tool_input(item)
        if event_type == "item.started":
            return [
                AgentEvent(
                    type="tool_use",
                    data={"tool_name": str(name), "tool_input": tool_input},
                )
            ]
        return [
            AgentEvent(
                type="tool_result",
                data={
                    "tool_result_id": _item_id(item),
                    "tool_result_content": _item_text(item),
                    "tool_result_is_error": _item_is_error(item),
                },
            )
        ]

    def _tool_input(self, item: dict[str, Any]) -> dict[str, Any]:
        tool_input = item.get("arguments") or item.get("input") or item.get("tool_input") or {}
        if isinstance(tool_input, str):
            with contextlib.suppress(json.JSONDecodeError):
                tool_input = json.loads(tool_input)
        if isinstance(tool_input, dict):
            return tool_input
        return {"input": tool_input}

    async def stop(self) -> None:
        """Interrupt any still-running Codex process."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            except Exception as exc:  # allow: exception-handling; cleanup; logged via _log()  # noqa: BLE001, RUF100
                _log(f"error during process cleanup: {exc}")
            finally:
                self._proc = None

    @property
    def session_id(self) -> str | None:
        """Return the current Codex thread/session ID."""
        return self._session_id
