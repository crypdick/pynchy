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
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

CONVERSATIONS_DIR = Path("/workspace/group/conversations")


class TranscriptMessage(TypedDict):
    """Archived user/assistant turn extracted from a JSONL transcript."""

    role: Literal["user", "assistant"]
    content: str


def _log(message: str) -> None:
    """Log to stderr (captured by the host container runner)."""
    sys.stderr.write(f"[transcript-archive] {message}\n")
    sys.stderr.flush()


def _sanitize_filename(summary: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:50]


def _generate_fallback_name() -> str:
    now = datetime.now(UTC)
    return f"conversation-{now.hour:02d}{now.minute:02d}"


def _extract_message_text(content: object, *, text_only: bool) -> str:
    """Extract displayable text from a transcript content payload."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if text_only and block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            text_parts.append(text)
    return "".join(text_parts)


def _parse_transcript_entry(entry: object) -> TranscriptMessage | None:
    """Parse a single JSON transcript entry into an archived message."""
    if not isinstance(entry, dict):
        return None

    message = entry.get("message")
    if not isinstance(message, dict):
        return None

    match entry.get("type"):
        case "user":
            role: Literal["user", "assistant"] = "user"
            text = _extract_message_text(message.get("content"), text_only=False)
        case "assistant":
            role = "assistant"
            text = _extract_message_text(message.get("content"), text_only=True)
        case _:
            return None

    if not text:
        return None
    return {"role": role, "content": text}


def _parse_transcript(content: str) -> list[TranscriptMessage]:
    """Parse JSONL transcript to archived user/assistant messages."""
    messages: list[TranscriptMessage] = []

    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        parsed = _parse_transcript_entry(entry)
        if parsed is not None:
            messages.append(parsed)

    return messages


def _format_transcript_markdown(messages: list[TranscriptMessage], title: str | None = None) -> str:
    """Format parsed messages as markdown."""
    now = datetime.now(UTC)
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
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index.get("entries", []):
            if entry.get("sessionId") == session_id:
                summary = entry.get("summary")
                return summary if isinstance(summary, str) else None
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"Failed to read sessions index: {exc}")

    return None


def _transcript_exists(transcript_path: str) -> bool:
    return Path(transcript_path).exists()


def _read_transcript(transcript_path: str) -> str:
    return Path(transcript_path).read_text(encoding="utf-8")


def _ensure_conversations_dir() -> None:
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _write_archive_file(file_path: Path, markdown: str) -> None:
    file_path.write_text(markdown, encoding="utf-8")


async def _build_archive_payload(
    transcript_path: str,
    session_id: str,
) -> tuple[Path, str] | None:
    content = await asyncio.to_thread(_read_transcript, transcript_path)
    messages = _parse_transcript(content)

    if not messages:
        _log("No messages to archive")
        return None

    summary = await asyncio.to_thread(_get_session_summary, session_id, transcript_path)
    name = _sanitize_filename(summary) if summary else _generate_fallback_name()

    await asyncio.to_thread(_ensure_conversations_dir)

    date = datetime.now(UTC).strftime("%Y-%m-%d")
    filename = f"{date}-{name}.md"
    file_path = CONVERSATIONS_DIR / filename
    markdown = _format_transcript_markdown(messages, summary)
    return file_path, markdown


async def archive_transcript(transcript_path: str, session_id: str) -> Path | None:
    """Archive a session transcript to markdown and structured memory.

    Returns the written file path, or ``None`` if there was nothing to archive.
    Never raises: archival is best-effort and must not disrupt compaction.
    """
    if not transcript_path or not await asyncio.to_thread(_transcript_exists, transcript_path):
        _log("No transcript found for archiving")
        return None

    try:
        payload = await _build_archive_payload(transcript_path, session_id)
        if payload is None:
            return None

        file_path, markdown = payload
        await asyncio.to_thread(_write_archive_file, file_path, markdown)
    except Exception as exc:  # allow: exception-handling; best-effort  # noqa: BLE001, RUF100
        _log(f"Failed to archive transcript: {exc}")
        return None
    else:
        _log(f"Archived conversation to {file_path}")

        # Best-effort: also save to structured memory for search
        try:
            from .agent_tools import (  # noqa: PLC0415, RUF100 - only needed for best-effort memory archival.
                request_host_service,
            )

            await request_host_service(
                "save_memory",
                {
                    "key": f"conversation-{file_path.stem}",
                    "content": markdown[:2000],
                    "category": "conversation",
                },
            )
        except Exception as exc:  # allow: exception-handling; best-effort  # noqa: BLE001, RUF100
            _log(f"save_memory IPC failed (non-fatal): {exc}")
        return file_path


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
    except Exception as exc:  # allow: exception-handling; gate fails open  # noqa: BLE001, RUF100
        _log(f"archive error, skipping: {exc}")

    sys.exit(0)


if __name__ == "__main__":
    main()
