"""Tests for transcript archival behavior.

These tests exercise the public archive hook and CLI entrypoint rather than
importing private helper functions from ``agent_runner.transcript_archive``.
That keeps the contract anchored on user-visible behavior: archived markdown,
filename selection, and the best-effort CLI wrapper.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent / "src" / "pynchy" / "agent" / "agent_runner" / "src")
)
from agent_runner import transcript_archive as ta


@pytest.fixture
def _isolated_archive(tmp_path, monkeypatch):
    """Redirect the archive dir to tmp and stub the best-effort memory IPC."""
    out_dir = tmp_path / "conversations"
    monkeypatch.setattr(ta, "CONVERSATIONS_DIR", out_dir)

    def _noop(*_args, **_kwargs):
        return asyncio.sleep(0)

    monkeypatch.setattr(
        "agent_runner.agent_tools._ipc_request.ipc_service_request", _noop, raising=False
    )
    return out_dir


def _write_transcript(dir_path: Path, lines: list[dict]) -> Path:
    tp = dir_path / "session.jsonl"
    tp.write_text("\n".join(json.dumps(entry) for entry in lines))
    return tp


class TestArchiveTranscript:
    """End-to-end behavior of archive_transcript."""

    @pytest.mark.usefixtures("_isolated_archive")
    def test_writes_markdown_and_returns_path(self, tmp_path):
        archive_dir = tmp_path / "conversations"
        tp = _write_transcript(
            tmp_path,
            [
                {"type": "user", "message": {"content": "hello there"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi!"}]}},
            ],
        )
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        assert out.exists()
        assert out.parent == archive_dir
        text = out.read_text()
        assert "# Conversation" in text
        assert "**User**: hello there" in text
        assert "**Pynchy**: hi!" in text

    @pytest.mark.usefixtures("_isolated_archive")
    def test_summary_drives_title_and_filename(self, tmp_path):
        tp = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
        (tmp_path / "sessions-index.json").write_text(
            json.dumps({"entries": [{"sessionId": "sid", "summary": "Weekly Sync Notes"}]})
        )
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        assert out.name.endswith("-weekly-sync-notes.md")
        assert "# Weekly Sync Notes" in out.read_text()

    @pytest.mark.usefixtures("_isolated_archive")
    def test_summary_is_sanitized_for_filename(self, tmp_path):
        tp = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
        summary = "Fix bug #123: crash on startup!"
        (tmp_path / "sessions-index.json").write_text(
            json.dumps({"entries": [{"sessionId": "sid", "summary": summary}]})
        )
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        assert out.name.endswith("-fix-bug-123-crash-on-startup.md")

    @pytest.mark.usefixtures("_isolated_archive")
    def test_fallback_name_without_summary(self, tmp_path):
        tp = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        assert "conversation-" in out.name
        assert "# Conversation" in out.read_text()

    @pytest.mark.usefixtures("_isolated_archive")
    def test_malformed_sessions_index_falls_back_to_generated_name(self, tmp_path):
        tp = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
        (tmp_path / "sessions-index.json").write_text("not valid json")
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        assert "conversation-" in out.name
        assert "# Conversation" in out.read_text()

    @pytest.mark.usefixtures("_isolated_archive")
    def test_multimodal_content_is_archived_as_text(self, tmp_path):
        tp = _write_transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Hello "},
                            {"type": "text", "text": "world"},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "bash", "input": {}},
                            {"type": "text", "text": "Done!"},
                        ]
                    },
                },
            ],
        )
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        text = out.read_text()
        assert "**User**: Hello world" in text
        assert "**Pynchy**: Done!" in text

    @pytest.mark.usefixtures("_isolated_archive")
    def test_long_content_is_truncated_in_markdown(self, tmp_path):
        content = "x" * 2001
        tp = _write_transcript(tmp_path, [{"type": "user", "message": {"content": content}}])
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        text = out.read_text()
        assert "x" * 2000 in text
        assert content not in text
        assert "..." in text

    @pytest.mark.usefixtures("_isolated_archive")
    def test_exactly_2000_chars_not_truncated(self, tmp_path):
        content = "x" * 2000
        tp = _write_transcript(tmp_path, [{"type": "user", "message": {"content": content}}])
        out = asyncio.run(ta.archive_transcript(str(tp), "sid"))
        assert out is not None
        text = out.read_text()
        assert content in text
        assert "..." not in text

    @pytest.mark.usefixtures("_isolated_archive")
    def test_tool_result_only_turns_skipped(self, tmp_path):
        """A transcript with no user/assistant text produces nothing to archive."""
        tp = _write_transcript(
            tmp_path,
            [
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
                },
                {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            ],
        )
        assert asyncio.run(ta.archive_transcript(str(tp), "sid")) is None

    @pytest.mark.usefixtures("_isolated_archive")
    def test_empty_transcript_returns_none(self, tmp_path):
        tp = tmp_path / "empty.jsonl"
        tp.write_text("")
        assert asyncio.run(ta.archive_transcript(str(tp), "sid")) is None

    @pytest.mark.usefixtures("_isolated_archive")
    def test_missing_transcript_returns_none(self, tmp_path):
        assert asyncio.run(ta.archive_transcript(str(tmp_path / "nope.jsonl"), "sid")) is None

    @pytest.mark.usefixtures("_isolated_archive")
    def test_blank_transcript_path_returns_none(self):
        assert asyncio.run(ta.archive_transcript("", "sid")) is None


class TestMain:
    """The PreCompact command-hook entrypoint (python -m agent_runner.transcript_archive)."""

    @pytest.mark.usefixtures("_isolated_archive")
    def test_valid_payload_archives_and_exits_zero(self, tmp_path, monkeypatch):
        archive_dir = tmp_path / "conversations"
        tp = _write_transcript(tmp_path, [{"type": "user", "message": {"content": "hi"}}])
        payload = json.dumps({"transcript_path": str(tp), "session_id": "sid"})
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        with pytest.raises(SystemExit) as exc:
            ta.main()
        assert exc.value.code == 0
        assert list(archive_dir.glob("*.md"))  # a file was written

    @pytest.mark.usefixtures("_isolated_archive")
    def test_malformed_stdin_exits_zero_without_writing(self, tmp_path, monkeypatch):
        archive_dir = tmp_path / "conversations"
        monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
        with pytest.raises(SystemExit) as exc:
            ta.main()
        assert exc.value.code == 0
        assert not archive_dir.exists() or not list(archive_dir.glob("*.md"))

    @pytest.mark.usefixtures("_isolated_archive")
    def test_empty_stdin_exits_zero(self, monkeypatch):
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        with pytest.raises(SystemExit) as exc:
            ta.main()
        assert exc.value.code == 0
