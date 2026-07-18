"""Tests for transcript archival."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent_runner.transcript_archive import archive_transcript

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_archive_transcript_writes_markdown_and_saves_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript_path = tmp_path / "transcript.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"content": "hello"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "hi there"}]},
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    index_path = tmp_path / "sessions-index.json"
    index_path.write_text(
        json.dumps({"entries": [{"sessionId": "session-1", "summary": "Weekly recap"}]}),
        encoding="utf-8",
    )

    conversations_dir = tmp_path / "conversations"
    monkeypatch.setattr("agent_runner.transcript_archive.CONVERSATIONS_DIR", conversations_dir)

    with patch(
        "agent_runner.agent_tools.request_host_service", new=AsyncMock(return_value=[])
    ) as save_memory:
        archived_path = await archive_transcript(str(transcript_path), "session-1")

    assert archived_path is not None
    assert archived_path.parent == conversations_dir
    assert archived_path.exists()

    markdown = archived_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Weekly recap")
    assert "**User**: hello" in markdown
    assert "**Pynchy**: hi there" in markdown
    save_memory.assert_awaited_once()
