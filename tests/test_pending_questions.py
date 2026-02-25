"""Tests for the pending question state manager."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    """Create and return a temporary IPC directory."""
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


# -- create_pending_question ---------------------------------------------------


class TestCreatePendingQuestion:
    def test_creates_pending_file(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import create_pending_question

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            create_pending_question(
                request_id="aabb001122334455",
                source_group="personal",
                chat_jid="slack:C123",
                channel_name="slack",
                session_id="sess-456",
                questions=[{"question": "Which auth?", "options": ["OAuth", "API key"]}],
            )

        pending_dir = ipc_dir / "personal" / "pending_questions"
        files = list(pending_dir.glob("*.json"))
        assert len(files) == 1
        assert files[0].name == "aabb001122334455.json"

        data = json.loads(files[0].read_text())
        assert data["request_id"] == "aabb001122334455"
        assert data["short_id"] == "aabb0011"
        assert data["source_group"] == "personal"
        assert data["chat_jid"] == "slack:C123"
        assert data["channel_name"] == "slack"
        assert data["session_id"] == "sess-456"
        assert data["questions"] == [{"question": "Which auth?", "options": ["OAuth", "API key"]}]
        assert data["message_id"] is None
        assert "timestamp" in data

    def test_atomic_write_no_tmp_left(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import create_pending_question

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            create_pending_question(
                request_id="abc123",
                source_group="grp",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[],
            )

        pending_dir = ipc_dir / "grp" / "pending_questions"
        assert not list(pending_dir.glob("*.tmp"))


# -- find_pending_question -----------------------------------------------------


class TestFindPendingQuestion:
    def test_finds_by_request_id(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import (
            create_pending_question,
            find_pending_question,
        )

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            create_pending_question(
                request_id="findme123",
                source_group="grp1",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[{"question": "Pick one"}],
            )
            result = find_pending_question("findme123")

        assert result is not None
        assert result["request_id"] == "findme123"
        assert result["source_group"] == "grp1"

    def test_finds_across_groups(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import (
            create_pending_question,
            find_pending_question,
        )

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            create_pending_question(
                request_id="in-grp2",
                source_group="grp2",
                chat_jid="slack:C2",
                channel_name="slack",
                session_id="sess-2",
                questions=[],
            )
            # Search should find it even though we don't specify the group
            result = find_pending_question("in-grp2")

        assert result is not None
        assert result["source_group"] == "grp2"

    def test_returns_none_when_missing(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import find_pending_question

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            result = find_pending_question("nonexistent")

        assert result is None

    def test_returns_none_when_no_ipc_dir(self, tmp_path: Path):
        """No ipc/ directory at all."""
        from pynchy.chat.pending_questions import find_pending_question

        s = make_settings(data_dir=tmp_path / "empty")
        with patch("pynchy.chat.pending_questions.get_settings", return_value=s):
            result = find_pending_question("anything")

        assert result is None


# -- resolve_pending_question --------------------------------------------------


class TestResolvePendingQuestion:
    def test_deletes_the_file(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import (
            create_pending_question,
            resolve_pending_question,
        )

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            create_pending_question(
                request_id="todelete",
                source_group="grp",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[],
            )
            resolve_pending_question("todelete", "grp")

        filepath = ipc_dir / "grp" / "pending_questions" / "todelete.json"
        assert not filepath.exists()

    def test_no_error_when_already_resolved(self, ipc_dir: Path, settings):
        """Resolving a nonexistent file should log a warning but not raise."""
        from pynchy.chat.pending_questions import resolve_pending_question

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            resolve_pending_question("ghost", "grp")  # should not raise


# -- update_message_id ---------------------------------------------------------


class TestUpdateMessageId:
    def test_updates_message_id(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import (
            create_pending_question,
            update_message_id,
        )

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            create_pending_question(
                request_id="msgupdate",
                source_group="grp",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[{"question": "Pick"}],
            )
            update_message_id("msgupdate", "grp", "ts:1234567890.123456")

        filepath = ipc_dir / "grp" / "pending_questions" / "msgupdate.json"
        data = json.loads(filepath.read_text())
        assert data["message_id"] == "ts:1234567890.123456"
        # Other fields should be preserved
        assert data["request_id"] == "msgupdate"
        assert data["questions"] == [{"question": "Pick"}]

    def test_atomic_write_no_tmp_left(self, ipc_dir: Path, settings):
        from pynchy.chat.pending_questions import (
            create_pending_question,
            update_message_id,
        )

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            create_pending_question(
                request_id="atomicup",
                source_group="grp",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[],
            )
            update_message_id("atomicup", "grp", "msg-999")

        pending_dir = ipc_dir / "grp" / "pending_questions"
        assert not list(pending_dir.glob("*.tmp"))

    def test_no_error_when_file_missing(self, ipc_dir: Path, settings):
        """Updating message_id on a nonexistent file should warn but not raise."""
        from pynchy.chat.pending_questions import update_message_id

        with patch("pynchy.chat.pending_questions.get_settings", return_value=settings):
            update_message_id("ghost", "grp", "msg-123")  # should not raise
