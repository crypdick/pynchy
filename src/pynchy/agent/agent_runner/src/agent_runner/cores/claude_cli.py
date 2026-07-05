"""Claude Code CLI agent core.

Drives the ``claude`` binary directly as a subprocess over the stream-json
protocol, instead of going through the Claude Agent SDK (see cores/claude.py).

Why this exists: the Agent SDK hands you already-parsed message objects and a
fixed menu of hooks. This core owns the subprocess and the stdout parse loop,
so every raw stream-json line passes through Python before it becomes an
``AgentEvent`` -- the seam for turn-by-turn control and arbitrary stream
injection. Select it with ``[agent] core = "claude-cli"`` (or
``PYNCHY_AGENT_CORE=claude-cli``).

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
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from ..core import AgentCoreConfig, AgentEvent

# stream-json lines can carry large tool results; lift the asyncio reader limit
# well above the 64 KiB default to avoid "chunk exceeded the limit" on big lines.
_STREAM_LINE_LIMIT = 32 * 1024 * 1024

# Built-in tools the agent may use (kept in sync with cores/claude.py).
_ALLOWED_TOOLS = [
    "Bash",
    "BashOutput",
    "KillBash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebSearch",
    "Task",
    "TaskOutput",
    "TaskStop",
    "TeamCreate",
    "TeamDelete",
    "SendMessage",
    "TodoWrite",
    "ToolSearch",
    "Skill",
    "NotebookEdit",
    "mcp__pynchy__*",
]

# Plan-mode / interactive tools that would hang a headless container
# (they require interactive approval; matches cores/claude.py).
_DISALLOWED_TOOLS = ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"]


def _log(message: str) -> None:
    """Log to stderr (captured by the host container runner)."""
    print(f"[claude-cli-core] {message}", file=sys.stderr, flush=True)  # allow: print-statements


class ClaudeCLIAgentCore:
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

        # MCP servers -> inline --mcp-config JSON: {"mcpServers": {...}}.
        if self.config.mcp_servers:
            self._mcp_config_json = json.dumps({"mcpServers": self.config.mcp_servers})

        # Settings: attribution off (match the SDK core) + a PreToolUse hook that
        # shells back into pynchy's Python security gate. The CLI passes the tool
        # call as JSON on the hook command's stdin and reads a decision on stdout.
        settings: dict[str, Any] = {
            "attribution": {"commit": "", "pr": ""},
            "hooks": {
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
                ]
            },
        }
        self._settings_json = json.dumps(settings)

        _log(f"claude binary: {self._claude_path}; MCP servers: {list(self.config.mcp_servers)}")

    def _allowed_tools(self) -> list[str]:
        """Built-in allow-list plus a wildcard per configured MCP server."""
        tools = list(_ALLOWED_TOOLS)
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
            *_DISALLOWED_TOOLS,
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
        context, prepend directives, or attach content blocks.
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
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        # Send the single user message, then EOF so the CLI stops reading input.
        proc.stdin.write(self._build_stdin(prompt))
        await proc.stdin.drain()
        proc.stdin.close()

        saw_result = False
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                _log(f"skipping non-JSON stdout line: {line[:200]}")
                continue
            for event in self._map_line(obj):
                if event.type == "result":
                    saw_result = True
                yield event

        stderr_text = (await proc.stderr.read()).decode(errors="replace")
        return_code = await proc.wait()
        self._proc = None

        # The runner contract requires query() to yield at least one "result"
        # event. If the CLI died before emitting one, synthesize an error result
        # so the turn terminates cleanly instead of hanging.
        if not saw_result:
            _log(
                f"claude exited rc={return_code} with no result event; stderr: {stderr_text[:500]}"
            )
            yield AgentEvent(
                type="result",
                data={
                    "result": (
                        f"claude CLI exited (code {return_code}) without a result. "
                        f"{stderr_text[:500]}"
                    ),
                    "result_metadata": {
                        "subtype": "error",
                        "is_error": True,
                        "session_id": self._session_id,
                    },
                },
            )

    def _map_line(self, obj: dict[str, Any]) -> list[AgentEvent]:
        """Map one stream-json line to zero or more ``AgentEvent``s.

        >>> INJECTION SEAM (output) <<<
        Every raw wire line passes through here before becoming an event.
        Rewrite, drop, or splice in synthetic events for turn-by-turn stream
        control. Enable ``--include-partial-messages`` in :meth:`_build_args`
        to also receive token-level ``stream_event`` deltas and handle them
        below.
        """
        msg_type = obj.get("type")
        events: list[AgentEvent] = []

        if msg_type == "system":
            subtype = obj.get("subtype")
            if subtype == "init":
                sid = obj.get("session_id")
                if sid:
                    self._session_id = sid
                    _log(f"session initialized: {sid}")
            events.append(
                AgentEvent(type="system", data={"system_subtype": subtype, "system_data": obj})
            )

        elif msg_type in ("assistant", "user"):
            # Assistant blocks are thinking/text/tool_use; tool results come back
            # inside "user" messages as tool_result blocks.
            content = (obj.get("message") or {}).get("content") or []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for block in content:
                if isinstance(block, dict):
                    events.extend(self._map_block(block))

        elif msg_type == "result":
            sid = obj.get("session_id")
            if sid:
                self._session_id = sid
            events.append(
                AgentEvent(
                    type="result",
                    data={
                        "result": obj.get("result"),
                        "result_metadata": {
                            "subtype": obj.get("subtype"),
                            "duration_ms": obj.get("duration_ms"),
                            "duration_api_ms": obj.get("duration_api_ms"),
                            "is_error": obj.get("is_error", False),
                            "num_turns": obj.get("num_turns"),
                            "session_id": sid,
                            "total_cost_usd": obj.get("total_cost_usd"),
                            "usage": obj.get("usage"),
                        },
                    },
                )
            )

        # msg_type == "stream_event" (partial messages) is intentionally ignored
        # unless --include-partial-messages is enabled; extend here to surface
        # token deltas.
        return events

    def _map_block(self, block: dict[str, Any]) -> list[AgentEvent]:
        """Map one content block to an ``AgentEvent`` (mirrors cores/claude.py)."""
        btype = block.get("type")
        if btype == "thinking":
            return [AgentEvent(type="thinking", data={"thinking": block.get("thinking", "")})]
        if btype == "text":
            return [AgentEvent(type="text", data={"text": block.get("text", "")})]
        if btype == "tool_use":
            return [
                AgentEvent(
                    type="tool_use",
                    data={"tool_name": block.get("name", ""), "tool_input": block.get("input", {})},
                )
            ]
        if btype == "tool_result":
            content = block.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content)
            elif not isinstance(content, str):
                content = ""
            return [
                AgentEvent(
                    type="tool_result",
                    data={
                        "tool_result_id": block.get("tool_use_id", ""),
                        "tool_result_content": content,
                        "tool_result_is_error": block.get("is_error", False),
                    },
                )
            ]
        return []

    async def stop(self) -> None:
        """Terminate any still-running turn process."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, ProcessLookupError):
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            except Exception as exc:  # allow: exception-handling — cleanup; logged via _log()
                _log(f"error during process cleanup: {exc}")
            finally:
                self._proc = None

    @property
    def session_id(self) -> str | None:
        """Return the current session ID (updated after each turn)."""
        return self._session_id
