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
from typing import TYPE_CHECKING

from agent_runner.events import (
    ResultEvent,
    ResultMetadata,
    SystemEvent,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)
from agent_runner.paths import AGENT_WORKSPACE

from ._codex_config import (
    DEFAULT_CODEX_SANDBOX_MODE,
    CodexModelSettings,
    gateway_base_url_from_env,
    write_codex_config,
)
from ._codex_event_parsing import (
    file_change_input,
    file_change_result,
    item_command,
    item_id,
    item_is_error,
    item_text,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_runner.core import AgentCoreConfig
    from agent_runner.events import AgentEvent

_STREAM_LINE_LIMIT = 32 * 1024 * 1024
_CODEX_SESSION_PREFIX = "codex:"
_CORE_NAME = "codex-cli"
_MISSING_STREAM_ERROR = "{core_name} subprocess missing {stream_name} stream after creation"


def _log(message: str) -> None:
    """Log to stderr (captured by the host container runner)."""
    sys.stderr.write(f"[codex-cli-core] {message}\n")
    sys.stderr.flush()


def _require_stream[TStream](stream: TStream | None, stream_name: str) -> TStream:
    if stream is None:
        raise RuntimeError(
            _MISSING_STREAM_ERROR.format(core_name=_CORE_NAME, stream_name=stream_name)
        )
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


def _configured_model(extra: dict[str, object]) -> str | None:
    model = extra.get("model")
    return str(model) if model else None


def _configured_model_reasoning_effort(extra: dict[str, object]) -> str | None:
    effort = extra.get("model_reasoning_effort")
    return effort if isinstance(effort, str) else None


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
        self._last_turn_metadata: dict[str, object] = {}
        self._turn_completed = False
        self._terminal_error_emitted = False
        self._pending_error: dict[str, object] | None = None

    async def start(self) -> None:
        """Resolve the CLI binary, write config, and prepare the environment."""
        self._codex_path = _resolve_codex_path()
        codex_home = _codex_home()
        gateway_base_url = gateway_base_url_from_env()
        write_codex_config(
            codex_home,
            self.config.mcp_servers,
            gateway_base_url=gateway_base_url,
            model_settings=CodexModelSettings(
                model=_configured_model(self.config.extra),
                model_reasoning_effort=_configured_model_reasoning_effort(self.config.extra),
            ),
            hooks_enabled=bool(self.config.extra.get("pynchy_hooks_enabled", True)),
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

        # Real Pynchy containers always create the agent workspace. Host-side wet
        # runs do not, so only add it when the runtime has actually mounted it.
        if self.config.cwd != str(AGENT_WORKSPACE) and AGENT_WORKSPACE.exists():
            args += ["--add-dir", str(AGENT_WORKSPACE)]

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
        """Build the raw stdin prompt for one Codex turn."""
        if self._session_id or not self.config.system_prompt_append:
            return (prompt + "\n").encode()
        return (f"{self.config.system_prompt_append}\n\nUser message:\n{prompt}\n").encode()

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Spawn one Codex turn and stream mapped events."""
        self._last_agent_message = None
        self._last_turn_metadata = {}
        self._turn_completed = False
        self._terminal_error_emitted = False
        self._pending_error = None
        _log(f"spawn codex (session: {self._session_id or 'new'})")
        proc = await self._spawn_process()
        await self._write_prompt(proc, prompt)

        saw_result = False
        async for event in self._stream_events(proc):
            if isinstance(event, ResultEvent):
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
        return self.map_stream_event(obj) if isinstance(obj, dict) else []

    async def _finish_process(self, proc: asyncio.subprocess.Process) -> tuple[str, int]:
        stderr = _require_stream(proc.stderr, "stderr")
        stderr_text = (await stderr.read()).decode(errors="replace")
        return_code = await proc.wait()
        self._proc = None
        return stderr_text, return_code

    def _synthesize_result(self, return_code: int, stderr_text: str) -> AgentEvent:
        if self._pending_error is not None:
            self._terminal_error_emitted = True
            return self._map_error_result(self._pending_error, "error")

        if return_code != 0:
            result = stderr_text.strip()[:500] or f"codex CLI exited (code {return_code})."
            return ResultEvent(
                result=result,
                result_metadata=ResultMetadata(
                    subtype="error",
                    is_error=True,
                    session_id=self._session_id,
                ),
            )

        if not self._turn_completed or not self._last_agent_message:
            _log(f"codex exited rc={return_code} without a terminal agent response")
            return ResultEvent(
                result=None,
                result_metadata=ResultMetadata(
                    subtype="missing_terminal_turn",
                    is_error=True,
                    session_id=self._session_id,
                ),
            )

        return ResultEvent(
            result=self._last_agent_message,
            result_metadata=ResultMetadata(
                subtype="success",
                is_error=False,
                session_id=self._session_id,
                extra=self._last_turn_metadata,
            ),
        )

    def map_stream_event(self, obj: dict[str, object]) -> list[AgentEvent]:
        """Map one Codex stream event to Pynchy's agent-event contract.

        This is the owned protocol boundary for ``codex exec --json``. It
        permits focused mapping tests without exposing subprocess lifecycle or
        internal event-dispatch helpers as test API.
        """
        event_type = obj.get("type")

        if event_type == "thread.started":
            return self._map_thread_started(obj)

        if event_type in {"turn.completed", "turn.completed_with_errors", "turn.failed", "error"}:
            return self._map_turn_event(str(event_type), obj)

        if event_type not in {"item.started", "item.completed"}:
            return []

        item = obj.get("item")
        if not isinstance(item, dict):
            return []
        return self._map_item_event(str(event_type), item)

    def _map_turn_event(self, event_type: str, obj: dict[str, object]) -> list[AgentEvent]:
        if event_type in {"turn.completed", "turn.completed_with_errors"}:
            self._turn_completed = True
            self._record_turn_metadata(obj)
            self._pending_error = None
            return []

        # Codex uses `error` for retry notices as well as terminal failures.
        # Retain those events as fallbacks; `turn.failed` is authoritative when
        # present. Without a terminal turn event, query() emits the latest
        # pending error at subprocess exit.
        if event_type == "error":
            if not self._terminal_error_emitted:
                self._pending_error = obj
            return []

        if self._terminal_error_emitted:
            return []
        self._terminal_error_emitted = True
        self._pending_error = None
        return [self._map_error_result(obj, event_type)]

    def _map_thread_started(self, obj: dict[str, object]) -> list[AgentEvent]:
        sid = obj.get("thread_id") or obj.get("threadId")
        if isinstance(sid, str) and sid:
            self._session_id = _pynchy_session_id(sid, _configured_model(self.config.extra))
        system_data = dict(obj)
        if self._session_id:
            system_data["session_id"] = self._session_id
        return [SystemEvent(system_subtype="thread.started", system_data=system_data)]

    def _record_turn_metadata(self, obj: dict[str, object]) -> None:
        self._last_turn_metadata = {
            key: value for key, value in obj.items() if key not in {"type", "items", "item"}
        }

    def _map_error_result(self, obj: dict[str, object], fallback_subtype: str) -> AgentEvent:
        err = obj.get("error") if isinstance(obj.get("error"), dict) else {}
        message = err.get("message") or obj.get("message") or "Codex turn failed"
        subtype = err.get("code") or obj.get("code") or fallback_subtype
        return ResultEvent(
            result=str(message),
            result_metadata=ResultMetadata(
                subtype=str(subtype), is_error=True, session_id=self._session_id
            ),
        )

    def _map_item_event(self, event_type: str, item: dict[str, object]) -> list[AgentEvent]:
        item_type = item.get("type") or item.get("item_type") or item.get("itemType")

        if item_type == "agent_message" and event_type == "item.completed":
            return self._map_agent_message_item(item)

        if item_type in {"reasoning", "reasoning_item"} and event_type == "item.completed":
            return self._map_reasoning_item(item)

        if item_type in {"command_execution", "command"}:
            return self._map_command_item(event_type, item)

        if item_type == "file_change":
            return self._map_file_change_item(event_type, item)

        if item_type in {"mcp_tool_call", "tool_call", "function_call"}:
            return self._map_tool_call_item(event_type, item, str(item_type))

        return []

    def _map_agent_message_item(self, item: dict[str, object]) -> list[AgentEvent]:
        text = item_text(item)
        if not text:
            return []
        self._last_agent_message = text
        return [TextEvent(text=text)]

    def _map_reasoning_item(self, item: dict[str, object]) -> list[AgentEvent]:
        text = item_text(item)
        if not text:
            return []
        return [ThinkingEvent(thinking=text)]

    def _map_command_item(self, event_type: str, item: dict[str, object]) -> list[AgentEvent]:
        command = item_command(item)
        if event_type == "item.started":
            return [ToolUseEvent(tool_name="Bash", tool_input={"command": command})]
        return [
            ToolResultEvent(
                tool_result_id=item_id(item),
                tool_result_content=item_text(item),
                tool_result_is_error=item_is_error(item),
            )
        ]

    def _map_file_change_item(self, event_type: str, item: dict[str, object]) -> list[AgentEvent]:
        tool_use = ToolUseEvent(tool_name="apply_patch", tool_input=file_change_input(item))
        if event_type == "item.started":
            return [tool_use]
        tool_result = ToolResultEvent(
            tool_result_id=item_id(item),
            tool_result_content=file_change_result(item),
            tool_result_is_error=item_is_error(item),
        )
        # `codex exec --json` emits successful file changes only as a completed
        # item, so synthesize the missing start before its result. A future
        # started item still maps normally without duplicating either event.
        return [tool_use, tool_result]

    def _map_tool_call_item(
        self, event_type: str, item: dict[str, object], fallback_name: str
    ) -> list[AgentEvent]:
        name = (
            item.get("tool_name")
            or item.get("toolName")
            or item.get("name")
            or item.get("function_name")
            or item.get("tool")
            or fallback_name
        )
        tool_input = self._tool_input(item)
        if event_type == "item.started":
            return [ToolUseEvent(tool_name=str(name), tool_input=tool_input)]
        return [
            ToolResultEvent(
                tool_result_id=item_id(item),
                tool_result_content=item_text(item),
                tool_result_is_error=item_is_error(item),
            )
        ]

    def _tool_input(self, item: dict[str, object]) -> dict[str, object]:
        tool_input = item.get("arguments") or item.get("input") or item.get("tool_input") or {}
        if isinstance(tool_input, str):
            with contextlib.suppress(json.JSONDecodeError):
                tool_input = json.loads(tool_input)
        if isinstance(tool_input, dict):
            server = item.get("server")
            if isinstance(server, str) and server:
                return {**tool_input, "server": server}
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
            # cleanup; logged via _log()
            except Exception as exc:  # allow: exception-handling  # noqa: BLE001
                _log(f"error during process cleanup: {exc}")
            finally:
                self._proc = None

    @property
    def session_id(self) -> str | None:
        """Return the current Codex thread/session ID."""
        return self._session_id
