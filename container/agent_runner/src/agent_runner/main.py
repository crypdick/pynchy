"""Pynchy Agent Runner — runs inside a container.

Port of container/agent-runner/src/index.ts.

Input protocol:
  Stdin: Full ContainerInput JSON (read until EOF)
  IPC:   Follow-up messages written as JSON files to /workspace/ipc/input/
         Sentinel: /workspace/ipc/input/_close — signals session end

Stdout protocol:
  Each result is wrapped in OUTPUT_START_MARKER / OUTPUT_END_MARKER pairs.

Known SDK differences from TypeScript:
  - No resumeSessionAt: Python SDK only supports resume by session_id, not by
    a specific message UUID within a session. Follow-up queries re-enter the
    session from the end, not from a specific assistant turn.
  - No mid-query message injection: TS uses a MessageStream (push-based
    AsyncIterable) to pipe IPC messages into an active query. The Python SDK's
    client.query()+receive_response() pattern processes messages between query
    rounds instead. Follow-up messages are queued and handled in the next loop
    iteration rather than injected live.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookContext,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

IPC_INPUT_DIR = Path("/workspace/ipc/input")
IPC_INPUT_CLOSE_SENTINEL = IPC_INPUT_DIR / "_close"
IPC_POLL_SECONDS = 0.5

OUTPUT_START_MARKER = "---PYNCHY_OUTPUT_START---"
OUTPUT_END_MARKER = "---PYNCHY_OUTPUT_END---"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class ContainerInput:
    def __init__(self, data: dict[str, Any]) -> None:
        self.prompt: str = data["prompt"]
        self.session_id: str | None = data.get("session_id")
        self.group_folder: str = data["group_folder"]
        self.chat_jid: str = data["chat_jid"]
        self.is_main: bool = data["is_main"]
        self.is_scheduled_task: bool = data.get("is_scheduled_task", False)
        self.system_notices: list[str] | None = data.get("system_notices")


class ContainerOutput:
    def __init__(
        self,
        status: str,
        result: str | None = None,
        new_session_id: str | None = None,
        error: str | None = None,
        *,
        type: str = "result",
        thinking: str | None = None,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        text: str | None = None,
        system_subtype: str | None = None,
        system_data: dict[str, Any] | None = None,
        tool_result_id: str | None = None,
        tool_result_content: str | None = None,
        tool_result_is_error: bool | None = None,
        result_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.result = result
        self.new_session_id = new_session_id
        self.error = error
        self.type = type
        self.thinking = thinking
        self.tool_name = tool_name
        self.tool_input = tool_input
        self.text = text
        self.system_subtype = system_subtype
        self.system_data = system_data
        self.tool_result_id = tool_result_id
        self.tool_result_content = tool_result_content
        self.tool_result_is_error = tool_result_is_error
        self.result_metadata = result_metadata

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "status": self.status}
        if self.type == "result":
            d["result"] = self.result
            if self.new_session_id:
                d["new_session_id"] = self.new_session_id
            if self.error:
                d["error"] = self.error
            if self.result_metadata:
                d["result_metadata"] = self.result_metadata
        elif self.type == "thinking":
            d["thinking"] = self.thinking
        elif self.type == "tool_use":
            d["tool_name"] = self.tool_name
            d["tool_input"] = self.tool_input
        elif self.type == "text":
            d["text"] = self.text
        elif self.type == "system":
            d["system_subtype"] = self.system_subtype
            d["system_data"] = self.system_data
        elif self.type == "tool_result":
            d["tool_result_id"] = self.tool_result_id
            d["tool_result_content"] = self.tool_result_content
            d["tool_result_is_error"] = self.tool_result_is_error
        return d


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def write_output(output: ContainerOutput) -> None:
    """Write a marker-wrapped output to stdout."""
    print(OUTPUT_START_MARKER)
    print(json.dumps(output.to_dict()))
    print(OUTPUT_END_MARKER)
    sys.stdout.flush()


def log(message: str) -> None:
    """Log to stderr (captured by host container runner)."""
    print(f"[agent-runner] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# IPC functions
# ---------------------------------------------------------------------------


def should_close() -> bool:
    """Check for _close sentinel."""
    if IPC_INPUT_CLOSE_SENTINEL.exists():
        with contextlib.suppress(OSError):
            IPC_INPUT_CLOSE_SENTINEL.unlink()
        return True
    return False


def drain_ipc_input() -> list[str]:
    """Drain all pending IPC input messages. Returns messages found."""
    try:
        IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(f for f in IPC_INPUT_DIR.iterdir() if f.suffix == ".json")

        messages: list[str] = []
        for file_path in files:
            try:
                data = json.loads(file_path.read_text())
                file_path.unlink()
                if data.get("type") == "message" and data.get("text"):
                    messages.append(data["text"])
            except Exception as exc:
                log(f"Failed to process input file {file_path.name}: {exc}")
                with contextlib.suppress(OSError):
                    file_path.unlink()
        return messages
    except Exception as exc:
        log(f"IPC drain error: {exc}")
        return []


async def wait_for_ipc_message() -> str | None:
    """Wait for a new IPC message or _close sentinel.

    Returns the messages as a single string, or None if _close.
    """
    while True:
        if should_close():
            return None
        messages = drain_ipc_input()
        if messages:
            return "\n".join(messages)
        await asyncio.sleep(IPC_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Session summary lookup
# ---------------------------------------------------------------------------


def get_session_summary(session_id: str, transcript_path: str) -> str | None:
    """Look up session summary from sessions-index.json."""
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"

    if not index_path.exists():
        log(f"Sessions index not found at {index_path}")
        return None

    try:
        index = json.loads(index_path.read_text())
        for entry in index.get("entries", []):
            if entry.get("sessionId") == session_id:
                return entry.get("summary")
    except Exception as exc:
        log(f"Failed to read sessions index: {exc}")

    return None


# ---------------------------------------------------------------------------
# Transcript archival (PreCompact hook)
# ---------------------------------------------------------------------------


def sanitize_filename(summary: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:50]


def generate_fallback_name() -> str:
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def parse_transcript(content: str) -> list[dict[str, str]]:
    """Parse JSONL transcript to messages."""
    messages: list[dict[str, str]] = []

    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if entry.get("type") == "user" and entry.get("message", {}).get("content"):
                raw = entry["message"]["content"]
                text = raw if isinstance(raw, str) else "".join(c.get("text", "") for c in raw)
                if text:
                    messages.append({"role": "user", "content": text})
            elif entry.get("type") == "assistant" and entry.get("message", {}).get("content"):
                text_parts = [
                    c.get("text", "")
                    for c in entry["message"]["content"]
                    if c.get("type") == "text"
                ]
                text = "".join(text_parts)
                if text:
                    messages.append({"role": "assistant", "content": text})
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    return messages


def format_transcript_markdown(messages: list[dict[str, str]], title: str | None = None) -> str:
    """Format parsed messages as markdown."""
    now = datetime.now()
    formatted_date = now.strftime("%b %d, %I:%M %p")

    lines = [
        f"# {title or 'Conversation'}",
        "",
        f"Archived: {formatted_date}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        sender = "User" if msg["role"] == "user" else "Pynchy"
        content = msg["content"][:2000] + "..." if len(msg["content"]) > 2000 else msg["content"]
        lines.append(f"**{sender}**: {content}")
        lines.append("")

    return "\n".join(lines)


def create_pre_compact_hook():
    """Create a PreCompact hook that archives the transcript."""

    async def hook(
        input_data: dict[str, Any],
        tool_use_id: str | None,
        context: HookContext,
    ) -> dict[str, Any]:
        transcript_path = input_data.get("transcript_path", "")
        session_id = input_data.get("session_id", "")

        if not transcript_path or not Path(transcript_path).exists():
            log("No transcript found for archiving")
            return {}

        try:
            content = Path(transcript_path).read_text()
            messages = parse_transcript(content)

            if not messages:
                log("No messages to archive")
                return {}

            summary = get_session_summary(session_id, transcript_path)
            name = sanitize_filename(summary) if summary else generate_fallback_name()

            conversations_dir = Path("/workspace/group/conversations")
            conversations_dir.mkdir(parents=True, exist_ok=True)

            date = datetime.now().strftime("%Y-%m-%d")
            filename = f"{date}-{name}.md"
            file_path = conversations_dir / filename

            markdown = format_transcript_markdown(messages, summary)
            file_path.write_text(markdown)

            log(f"Archived conversation to {file_path}")
        except Exception as exc:
            log(f"Failed to archive transcript: {exc}")

        return {}

    return hook


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    # Read input from stdin
    try:
        stdin_data = sys.stdin.read()
        container_input = ContainerInput(json.loads(stdin_data))
        log(f"Received input for group: {container_input.group_folder}")
    except Exception as exc:
        write_output(
            ContainerOutput(
                status="error",
                error=f"Failed to parse input: {exc}",
            )
        )
        sys.exit(1)

    # Clean up stale _close sentinel
    IPC_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        IPC_INPUT_CLOSE_SENTINEL.unlink()

    # Build initial prompt (drain any pending IPC messages too)
    prompt = container_input.prompt
    if container_input.is_scheduled_task:
        prompt = (
            "[SCHEDULED TASK - The following message was sent automatically "
            "and is not coming directly from the user or group.]\n\n" + prompt
        )
    pending = drain_ipc_input()
    if pending:
        log(f"Draining {len(pending)} pending IPC messages into initial prompt")
        prompt += "\n" + "\n".join(pending)

    # Load global CLAUDE.md as additional system context (non-main groups only)
    global_claude_md_path = Path("/workspace/global/CLAUDE.md")
    system_prompt: dict[str, Any] | None = None
    if not container_input.is_main and global_claude_md_path.exists():
        global_claude_md = global_claude_md_path.read_text()
        system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": global_claude_md,
        }

    # Append system notices (e.g., git health warnings from host)
    if container_input.system_notices:
        notices_text = "\n\n".join(container_input.system_notices)
        if system_prompt is None:
            system_prompt = {
                "type": "preset",
                "preset": "claude_code",
                "append": notices_text,
            }
        else:
            system_prompt["append"] += "\n\n" + notices_text

    # MCP server path for IPC tools
    mcp_server_command = "python"
    mcp_server_args = ["-m", "agent_runner.ipc_mcp"]

    # Build mcp_servers dict starting with built-in pynchy server
    mcp_servers_dict = {
        "pynchy": {
            "command": mcp_server_command,
            "args": mcp_server_args,
            "env": {
                "PYNCHY_CHAT_JID": container_input.chat_jid,
                "PYNCHY_GROUP_FOLDER": container_input.group_folder,
                "PYNCHY_IS_MAIN": ("1" if container_input.is_main else "0"),
                "PYNCHY_SESSION_ID": (container_input.session_id or ""),
            },
        },
    }

    # Merge plugin MCP servers
    if container_input.plugin_mcp_servers:
        for name, spec in container_input.plugin_mcp_servers.items():
            plugin_env = spec.get("env", {}).copy()
            # Add PYTHONPATH for plugin source imports
            plugin_env["PYTHONPATH"] = f"/workspace/plugins/{name}"
            mcp_servers_dict[name] = {
                "command": spec["command"],
                "args": spec["args"],
                "env": plugin_env,
            }

    options = ClaudeAgentOptions(
        cwd="/workspace/group",
        resume=container_input.session_id,
        system_prompt=system_prompt,
        allowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
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
        ],
        permission_mode="bypassPermissions",
        setting_sources=["project", "user"],
        mcp_servers=mcp_servers_dict,
        hooks={
            "PreCompact": [HookMatcher(hooks=[create_pre_compact_hook()])],
        },
    )

    session_id = container_input.session_id

    try:
        async with ClaudeSDKClient(options) as client:
            while True:
                log(f"Starting query (session: {session_id or 'new'})...")

                await client.query(prompt)

                new_session_id: str | None = None
                message_count = 0
                result_count = 0
                closed_during_query = False

                async for message in client.receive_response():
                    message_count += 1

                    # Check for close during query
                    if should_close():
                        log("Close sentinel detected during query")
                        closed_during_query = True

                    # Emit all SystemMessages for transparent token stream
                    if isinstance(message, SystemMessage):
                        if message.subtype == "init" and hasattr(message, "data"):
                            sid = message.data.get("session_id")
                            if sid:
                                new_session_id = sid
                                log(f"Session initialized: {new_session_id}")
                        write_output(
                            ContainerOutput(
                                status="success",
                                type="system",
                                system_subtype=message.subtype,
                                system_data=message.data if hasattr(message, "data") else {},
                            )
                        )

                    # Emit trace blocks from assistant messages
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, ThinkingBlock):
                                write_output(
                                    ContainerOutput(
                                        status="success",
                                        type="thinking",
                                        thinking=block.thinking,
                                    )
                                )
                            elif isinstance(block, ToolUseBlock):
                                write_output(
                                    ContainerOutput(
                                        status="success",
                                        type="tool_use",
                                        tool_name=block.name,
                                        tool_input=block.input,
                                    )
                                )
                            elif isinstance(block, ToolResultBlock):
                                # Flatten content to string for storage
                                if isinstance(block.content, str):
                                    content_str = block.content
                                elif isinstance(block.content, list):
                                    content_str = json.dumps(block.content)
                                else:
                                    content_str = ""
                                write_output(
                                    ContainerOutput(
                                        status="success",
                                        type="tool_result",
                                        tool_result_id=block.tool_use_id,
                                        tool_result_content=content_str,
                                        tool_result_is_error=block.is_error,
                                    )
                                )
                            elif isinstance(block, TextBlock):
                                write_output(
                                    ContainerOutput(
                                        status="success",
                                        type="text",
                                        text=block.text,
                                    )
                                )

                    # Emit results
                    if isinstance(message, ResultMessage):
                        result_count += 1
                        text_result = getattr(message, "result", None)
                        log(
                            f"Result #{result_count}: "
                            f"subtype={message.subtype}"
                            f"{f' text={text_result[:200]}' if text_result else ''}"
                        )
                        result_meta = {
                            "subtype": message.subtype,
                            "duration_ms": message.duration_ms,
                            "duration_api_ms": message.duration_api_ms,
                            "is_error": message.is_error,
                            "num_turns": message.num_turns,
                            "session_id": message.session_id,
                            "total_cost_usd": message.total_cost_usd,
                            "usage": message.usage,
                        }
                        write_output(
                            ContainerOutput(
                                status="success",
                                result=text_result,
                                new_session_id=new_session_id,
                                result_metadata=result_meta,
                            )
                        )

                if new_session_id:
                    session_id = new_session_id

                log(
                    f"Query done. Messages: {message_count}, "
                    f"results: {result_count}, "
                    f"closedDuringQuery: {closed_during_query}"
                )

                # If _close was consumed during the query, exit immediately
                if closed_during_query:
                    log("Close sentinel consumed during query, exiting")
                    break

                # Emit session update so host can track it
                write_output(
                    ContainerOutput(
                        status="success",
                        result=None,
                        new_session_id=session_id,
                    )
                )

                log("Query ended, waiting for next IPC message...")

                next_message = await wait_for_ipc_message()
                if next_message is None:
                    log("Close sentinel received, exiting")
                    break

                log(f"Got new message ({len(next_message)} chars), starting new query")
                prompt = next_message

    except Exception as exc:
        error_message = str(exc)
        log(f"Agent error: {error_message}")
        write_output(
            ContainerOutput(
                status="error",
                new_session_id=session_id,
                error=error_message,
            )
        )
        sys.exit(1)
