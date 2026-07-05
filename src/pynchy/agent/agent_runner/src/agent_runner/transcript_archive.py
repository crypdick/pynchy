"""Shared transcript archival for the PreCompact lifecycle event.

Both agent cores archive the conversation before Claude auto-compacts it: the
SDK core (cores/claude.py) registers this as an in-process ``PreCompact`` hook,
and the claude-cli core wires it as a ``PreCompact`` command hook that runs
``python -m agent_runner.transcript_archive`` (see ``main`` below). Keeping the
logic here is the single source of truth so the two cores can't drift.

Given a session's JSONL transcript path, this parses the user/assistant turns,
writes a markdown copy to ``/workspace/group/conversations``, and (best-effort)
saves a snippet to structured memory over file IPC.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

CONVERSATIONS_DIR = Path("/workspace/group/conversations")


def _log(message: str) -> None:
    """Log to stderr (captured by the host container runner)."""
    print(f"[transcript-archive] {message}", file=sys.stderr, flush=True)  # allow: print-statements


def _sanitize_filename(summary: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:50]


def _generate_fallback_name() -> str:
    now = datetime.now()
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def _parse_transcript(content: str) -> list[dict[str, str]]:
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


def _format_transcript_markdown(messages: list[dict[str, str]], title: str | None = None) -> str:
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


def _get_session_summary(session_id: str, transcript_path: str) -> str | None:
    """Look up session summary from sessions-index.json."""
    project_dir = Path(transcript_path).parent
    index_path = project_dir / "sessions-index.json"

    if not index_path.exists():
        _log(f"Sessions index not found at {index_path}")
        return None

    try:
        index = json.loads(index_path.read_text())
        for entry in index.get("entries", []):
            if entry.get("sessionId") == session_id:
                return entry.get("summary")
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"Failed to read sessions index: {exc}")

    return None


async def archive_transcript(transcript_path: str, session_id: str) -> Path | None:
    """Archive a session transcript to markdown and structured memory.

    Returns the written file path, or ``None`` if there was nothing to archive.
    Never raises: archival is best-effort and must not disrupt compaction.
    """
    if not transcript_path or not Path(transcript_path).exists():
        _log("No transcript found for archiving")
        return None

    try:
        content = Path(transcript_path).read_text()
        messages = _parse_transcript(content)

        if not messages:
            _log("No messages to archive")
            return None

        summary = _get_session_summary(session_id, transcript_path)
        name = _sanitize_filename(summary) if summary else _generate_fallback_name()

        CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)

        date = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date}-{name}.md"
        file_path = CONVERSATIONS_DIR / filename

        markdown = _format_transcript_markdown(messages, summary)
        file_path.write_text(markdown)

        _log(f"Archived conversation to {file_path}")

        # Best-effort: also save to structured memory for search
        try:
            from agent_runner.agent_tools._ipc_request import ipc_service_request

            await ipc_service_request(
                "save_memory",
                {
                    "key": f"conversation-{date}-{name}",
                    "content": markdown[:2000],
                    "category": "conversation",
                },
            )
        except Exception as exc:  # allow: exception-handling — best-effort; logged via _log()
            _log(f"save_memory IPC failed (non-fatal): {exc}")

        return file_path
    except Exception as exc:  # allow: exception-handling — best-effort; logged via _log()
        _log(f"Failed to archive transcript: {exc}")
        return None


def main() -> None:
    """CLI ``PreCompact`` hook entrypoint for the claude-cli agent core.

    The Claude Code CLI passes the hook payload as JSON on stdin (fields include
    ``transcript_path`` and ``session_id``). We archive and exit 0 regardless --
    a PreCompact hook that errors or blocks would stall compaction, and archival
    is strictly best-effort. Invoked as ``python -m agent_runner.transcript_archive``.
    """
    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        _log(f"unparseable hook input, skipping archive: {raw[:200]}")
        sys.exit(0)

    transcript_path = data.get("transcript_path", "")
    session_id = data.get("session_id", "")

    try:
        asyncio.run(archive_transcript(transcript_path, session_id))
    except Exception as exc:  # allow: exception-handling — gate fails open; logged via _log()
        _log(f"archive error, skipping: {exc}")

    sys.exit(0)


if __name__ == "__main__":
    main()
