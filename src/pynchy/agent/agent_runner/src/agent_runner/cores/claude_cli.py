"""Claude Code CLI agent core.

Drives the ``claude`` binary directly as a subprocess over the stream-json
protocol, instead of going through the Claude Agent SDK (see cores/claude.py).

Why this exists: the Agent SDK hands you already-parsed message objects and a
fixed menu of hooks. This core owns the subprocess and the stdout parse loop,
so every raw stream-json line passes through Python before it becomes an
``AgentEvent`` -- the seam for turn-by-turn control and arbitrary stream
injection. Select it with ``[agent] default_core = "claude-cli"`` (or
``AGENT__DEFAULT_CORE=claude-cli``).

Everything else reuses pynchy's existing wiring: the LiteLLM gateway via the
inherited ``ANTHROPIC_*`` env, the file-IPC ``AgentEvent`` stream, MCP config,
the session-id pointer, and the shared BEFORE_TOOL_USE security gate -- wired as
a CLI ``PreToolUse`` hook that shells into the same Python functions the SDK
core uses (see security/hook_entry.py). The gate's taint state lives host-side
and is reached over file IPC, so the subprocess hook is functionally identical
to the SDK's in-process hook.
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

from agent_runner.events import (
    ResultEvent,
    ResultMetadata,
    SystemEvent,
    TextEvent,
    ThinkingEvent,
    ToolResultEvent,
    ToolUseEvent,
)

from .tools import BUILTIN_ALLOWED_TOOLS, DISALLOWED_TOOLS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from agent_runner.core import AgentCoreConfig
    from agent_runner.events import AgentEvent

# stream-json lines can carry large tool results; lift the asyncio reader limit
# well above the 64 KiB default to avoid "chunk exceeded the limit" on big lines.
_STREAM_LINE_LIMIT = 32 * 1024 * 1024
_CORE_NAME = "claude-cli"
_MISSING_STREAM_ERROR = "{core_name} subprocess missing {stream_name} stream after creation"


def _log(message: str) -> None:
    """Log to stderr (captured by the host container runner)."""
    sys.stderr.write(f"[claude-cli-core] {message}\n")
    sys.stderr.flush()


def _require_stream[TStream](stream: TStream | None, stream_name: str) -> TStream:
    if stream is None:
        raise RuntimeError(
            _MISSING_STREAM_ERROR.format(core_name=_CORE_NAME, stream_name=stream_name)
        )
    return stream


class ClaudeCLIAgentCore:  # noqa: V102
    """Agent core that drives the ``claude`` CLI as a subprocess.

    One ``claude --print`` process is spawned per :meth:`query` (one turn), with
    ``--resume`` carrying session continuity between turns -- the same one-shot
    model the CLI is designed around. ``start()`` resolves the binary and builds
    the static settings/env; ``stop()`` reaps any stray process.
    """

    def __init__(self, config: AgentCoreConfig) -> None:
        self.config = config
        self._session_id: str | None = config.session_id
        self._claude_path: str = "claude"
        self._settings_json: str = "{}"
        self._mcp_config_json: str | None = None
        self._env: dict[str, str] | None = None
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        """Resolve the CLI binary and build the static invocation config."""
        self._claude_path = shutil.which("claude") or "claude"
        # Inherit the container env: ANTHROPIC_BASE_URL (LiteLLM gateway), auth
        # token, PYTHONPATH (so the PreToolUse hook can import agent_runner).
        self._env = os.environ.copy()

        # The PreToolUse gate runs as a *fresh subprocess* (hook_entry.py) that
        # can't see our in-memory config, so plugin BEFORE_TOOL_USE specs travel
        # to it via this env var (inherited by the hook command the claude binary
        # spawns). hook_entry composes the same before_tool_use_roster the SDK
        # core does -- builtin + these -- so the gate can't differ by core.
        self._env["PYNCHY_PLUGIN_HOOKS"] = json.dumps(self.config.plugin_hooks)

        # MCP servers -> inline --mcp-config JSON: {"mcpServers": {...}}.
        if self.config.mcp_servers:
            self._mcp_config_json = json.dumps({"mcpServers": self.config.mcp_servers})

        # Settings: attribution off (match the SDK core) + two command hooks that
        # shell back into pynchy's Python, mirroring what the SDK core registers
        # in-process (cores/claude.py). The CLI passes the hook payload as JSON on
        # each command's stdin.
        #   - PreToolUse -> the shared BEFORE_TOOL_USE security gate; reads a
        #     decision back on stdout (see security/hook_entry.py).
        #   - PreCompact -> archives the transcript before auto-compaction, the
        #     same behavior the SDK core gets from its PreCompact hook
        #     (see transcript_archive.py).
        settings: dict[str, Any] = {"attribution": {"commit": "", "pr": ""}}
        if bool(self.config.extra.get("pynchy_hooks_enabled", True)):
            settings["hooks"] = {
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    f"{sys.executable} -m agent_runner.security.hook_entry"
                                ),
                            }
                        ],
                    }
                ],
                "PreCompact": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": (f"{sys.executable} -m agent_runner.transcript_archive"),
                            }
                        ],
                    }
                ],
            }
        self._settings_json = json.dumps(settings)

        _log(f"claude binary: {self._claude_path}; MCP servers: {list(self.config.mcp_servers)}")

    def _allowed_tools(self) -> list[str]:
        """Built-in allow-list plus a wildcard per configured MCP server."""
        tools = list(BUILTIN_ALLOWED_TOOLS)
        for server_name in self.config.mcp_servers:
            pattern = f"mcp__{server_name}__*"
            if pattern not in tools:
                tools.append(pattern)
        return tools

    def _build_args(self) -> list[str]:
        """Assemble the ``claude`` argv for one turn.

        Variadic flags (``--allowedTools``/``--disallowedTools``/``--mcp-config``)
        are each terminated by the next ``--flag``, so ordering matters: keep a
        ``--`` flag immediately after every variadic value list.
        """
        args: list[str] = [
            self._claude_path,
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            str(self.config.extra.get("model", "opus")),
            "--permission-mode",
            "bypassPermissions",
            "--settings",
            self._settings_json,
            "--setting-sources",
            "project,user",
            "--allowedTools",
            *self._allowed_tools(),
            "--disallowedTools",
            *DISALLOWED_TOOLS,
        ]
        if self._session_id:
            args += ["--resume", self._session_id]
        if self.config.system_prompt_append:
            args += ["--append-system-prompt", self.config.system_prompt_append]
        if self._mcp_config_json:
            args += ["--mcp-config", self._mcp_config_json]
        # Load Claude Code plugins baked into the image (the SDK core does the same).
        plugins_dir = Path("/opt/plugins")
        if plugins_dir.is_dir():
            for plugin in sorted(plugins_dir.iterdir()):
                if plugin.is_dir():
                    args += ["--plugin-dir", str(plugin)]
        return args

    def _build_stdin(self, prompt: str) -> bytes:
        """Build the stream-json user message written to the CLI's stdin.

        >>> INJECTION SEAM (input) <<<
        Rewrite or wrap ``prompt`` here before it reaches the model -- the
        turn-by-turn input control the Agent SDK does not expose. You can inject
        context, prepend prompts, or attach content blocks.
        """
        payload = {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        }
        return (json.dumps(payload) + "\n").encode()

    async def query(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Spawn one ``claude --print`` turn and stream its events."""
        args = self._build_args()
        _log(f"spawn claude (session: {self._session_id or 'new'})")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.cwd,
            env=self._env,
            limit=_STREAM_LINE_LIMIT,
        )
        self._proc = proc
        stdin = _require_stream(proc.stdin, "stdin")
        stdout = _require_stream(proc.stdout, "stdout")
        stderr = _require_stream(proc.stderr, "stderr")

        # Send the single user message, then EOF so the CLI stops reading input.
        stdin.write(self._build_stdin(prompt))
        await stdin.drain()
        stdin.close()

        saw_result = False
        async for raw in stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                _log(f"skipping non-JSON stdout line: {line[:200]}")
                continue
            for event in self.map_stream_line(obj):
                if event.type == "result":
                    saw_result = True
                yield event

        stderr_text = (await stderr.read()).decode(errors="replace")
        return_code = await proc.wait()
        self._proc = None

        # The runner contract requires query() to yield at least one "result"
        # event. If the CLI died before emitting one, synthesize an error result
        # so the turn terminates cleanly instead of hanging.
        if not saw_result:
            _log(
                f"claude exited rc={return_code} with no result event; stderr: {stderr_text[:500]}"
            )
            yield ResultEvent(
                result=(
                    f"claude CLI exited (code {return_code}) without a result. {stderr_text[:500]}"
                ),
                result_metadata=ResultMetadata(
                    subtype="error", is_error=True, session_id=self._session_id
                ),
            )

    def map_stream_line(self, obj: dict[str, Any]) -> list[AgentEvent]:
        """Map one Claude CLI stream-json line to zero or more ``AgentEvent``s.

        This is the owned boundary for Claude's external stream protocol.
        It lets adapters and integration tests turn a received line into the
        normal agent event contract without depending on the subprocess loop.
        """
        msg_type = obj.get("type")
        if msg_type == "system":
            return self._map_system_line(obj)

        if msg_type in ("assistant", "user"):
            return self._map_message_line(msg_type, obj)

        if msg_type == "result":
            return [self._result_event(obj)]

        # msg_type == "stream_event" (partial messages) is intentionally ignored
        # unless --include-partial-messages is enabled; extend here to surface
        # token deltas.
        return []

    def _map_system_line(self, obj: dict[str, Any]) -> list[AgentEvent]:
        """Map a system stream line and update session state when needed."""
        subtype = obj.get("subtype")
        if subtype == "init":
            sid = obj.get("session_id")
            if sid:
                self._session_id = sid
                _log(f"session initialized: {sid}")
        return [SystemEvent(system_subtype=str(subtype), system_data=obj)]

    def _map_message_line(self, msg_type: str, obj: dict[str, Any]) -> list[AgentEvent]:
        """Map assistant/user message content blocks into AgentEvents."""
        content = self._message_blocks(msg_type, obj)
        events: list[AgentEvent] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if msg_type == "user" and block.get("type") != "tool_result":
                continue
            events.extend(self._map_block(block))
        return events

    def _message_blocks(self, msg_type: str, obj: dict[str, Any]) -> list[Any]:
        """Normalize message content into block dictionaries."""
        # Assistant messages carry thinking/text/tool_use blocks; "user"
        # messages on this stream carry only tool_result blocks (verified
        # against claude CLI stream-json output -- the input prompt is *not*
        # echoed back on stdout). Restrict user-message mapping to
        # tool_result so a future CLI that echoes a text turn can't surface
        # the human's own prompt as an agent "text" event.
        content = (obj.get("message") or {}).get("content") or []
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if msg_type == "assistant" else []
        return content if isinstance(content, list) else []

    def _result_event(self, obj: dict[str, Any]) -> AgentEvent:
        """Build the terminal result event and update session state."""
        sid = obj.get("session_id")
        if sid:
            self._session_id = sid
        result = obj.get("result")
        return ResultEvent(
            result=result if isinstance(result, str) else None,
            result_metadata=ResultMetadata(
                subtype=str(obj.get("subtype") or "result"),
                is_error=bool(obj.get("is_error")),
                session_id=sid if isinstance(sid, str) else None,
                extra={
                    key: obj[key]
                    for key in (
                        "duration_ms",
                        "duration_api_ms",
                        "num_turns",
                        "total_cost_usd",
                        "usage",
                    )
                    if key in obj
                },
            ),
        )

    def _map_block(self, block: dict[str, Any]) -> list[AgentEvent]:
        """Map one content block to an ``AgentEvent`` (mirrors cores/claude.py)."""
        btype = block.get("type")
        if btype == "thinking":
            return [ThinkingEvent(thinking=str(block.get("thinking", "")))]
        if btype == "text":
            return [TextEvent(text=str(block.get("text", "")))]
        if btype == "tool_use":
            return [
                ToolUseEvent(
                    tool_name=str(block.get("name", "")),
                    tool_input=(
                        block.get("input", {}) if isinstance(block.get("input", {}), dict) else {}
                    ),
                )
            ]
        if btype == "tool_result":
            content = block.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content)
            elif not isinstance(content, str):
                content = ""
            return [
                ToolResultEvent(
                    tool_result_id=str(block.get("tool_use_id", "")),
                    tool_result_content=content,
                    tool_result_is_error=bool(block.get("is_error")),
                )
            ]
        return []

    async def stop(self) -> None:
        """Interrupt any still-running turn process.

        Send SIGINT first (what Ctrl+C sends): the ``claude`` CLI treats it as a
        graceful interrupt and checkpoints its session JSONL, so the next
        ``--resume`` sees a consistent transcript. Escalate to SIGKILL only if it
        doesn't exit in time.
        """
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
        """Return the current session ID (updated after each turn)."""
        return self._session_id
