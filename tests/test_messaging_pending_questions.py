"""Tests for the pending question state manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.host.container_manager.ipc.write import ipc_response_path, write_ipc_response
from pynchy.host.orchestrator.messaging.pending_questions import (
    create_pending_question,
    find_pending_for_jid,
    find_pending_question,
    resolve_pending_question,
    sweep_expired_questions,
    update_message_id,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def ipc_dir(tmp_path: Path) -> Path:
    """Create and return a temporary IPC directory."""
    d = tmp_path / "ipc"
    d.mkdir()
    return d


@pytest.fixture
def settings(tmp_path: Path):
    return make_settings(data_dir=tmp_path)


def _write_expiration_response(group_name: str, request_id: str, error: str) -> None:
    write_ipc_response(ipc_response_path(group_name, request_id), {"error": error})


# -- create_pending_question ---------------------------------------------------


class TestCreatePendingQuestion:
    def test_creates_pending_file(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
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
        expected_fields = {
            "request_id": "aabb001122334455",
            "short_id": "aabb0011",
            "source_group": "personal",
            "chat_jid": "slack:C123",
            "channel_name": "slack",
            "session_id": "sess-456",
            "questions": [{"question": "Which auth?", "options": ["OAuth", "API key"]}],
            "message_id": None,
        }
        for field_name, expected_value in expected_fields.items():
            assert data[field_name] == expected_value
        assert "timestamp" in data

    def test_atomic_write_no_tmp_left(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
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

    def test_rejects_request_id_that_escapes_pending_directory(
        self, ipc_dir: Path, settings
    ) -> None:
        victim = ipc_dir / "victim.json"
        victim.write_text("keep me")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            pytest.raises(ValueError, match="safe path component"),
        ):
            create_pending_question(
                request_id="../../victim",
                source_group="grp",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[],
            )

        assert victim.read_text() == "keep me"


# -- find_pending_question -----------------------------------------------------


class TestFindPendingQuestion:
    def test_returns_none_for_unsafe_request_id(self):
        assert find_pending_question("../../victim") is None

    def test_requires_configured_ipc_root(self):
        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                None,
            ),
            pytest.raises(RuntimeError, match="IPC base directory has not been configured"),
        ):
            find_pending_question("anything")

    def test_finds_by_request_id(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
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
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
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
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            result = find_pending_question("nonexistent")

        assert result is None

    def test_returns_none_when_no_ipc_dir(self, tmp_path: Path):
        """No ipc/ directory at all."""
        s = make_settings(data_dir=tmp_path / "empty")
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            s.data_dir / "ipc",
        ):
            result = find_pending_question("anything")

        assert result is None

    def test_skips_non_groups_missing_directories_and_corrupt_files(self, ipc_dir: Path, settings):
        (ipc_dir / "errors").mkdir()
        (ipc_dir / "not-a-group").write_text("not a directory")
        (ipc_dir / "group-without-pending").mkdir()
        corrupt_dir = ipc_dir / "corrupt-group" / "pending_questions"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "bad.json").write_text("{not json")

        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            assert find_pending_question("bad") is None


class TestFindPendingForJid:
    def test_returns_none_when_no_ipc_dir(self, tmp_path: Path):
        root = tmp_path / "missing-ipc"
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            root,
        ):
            assert find_pending_for_jid("slack:C1") is None

    def test_skips_non_groups_missing_directories_and_corrupt_files(self, ipc_dir: Path, settings):
        (ipc_dir / "errors").mkdir()
        (ipc_dir / "not-a-group").write_text("not a directory")
        (ipc_dir / "group-without-pending").mkdir()
        corrupt_dir = ipc_dir / "corrupt-group" / "pending_questions"
        corrupt_dir.mkdir(parents=True)
        (corrupt_dir / "bad.json").write_text("{not json")

        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            assert find_pending_for_jid("slack:C1") is None


# -- resolve_pending_question --------------------------------------------------


class TestResolvePendingQuestion:
    def test_deletes_the_file(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
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
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            resolve_pending_question("ghost", "grp")  # should not raise

    def test_rejects_request_id_that_escapes_pending_directory(
        self, ipc_dir: Path, settings
    ) -> None:
        victim = ipc_dir / "victim.json"
        victim.write_text("keep me")

        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            pytest.raises(ValueError, match="safe path component"),
        ):
            resolve_pending_question("../../victim", "grp")

        assert victim.read_text() == "keep me"


# -- update_message_id ---------------------------------------------------------


class TestUpdateMessageId:
    def test_ignores_corrupt_pending_file(self, ipc_dir: Path, settings):
        pending_dir = ipc_dir / "grp" / "pending_questions"
        pending_dir.mkdir(parents=True)
        (pending_dir / "corrupt.json").write_text("{not json")

        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            update_message_id("corrupt", "grp", "msg-123")

        assert (pending_dir / "corrupt.json").read_text() == "{not json"

    def test_updates_message_id(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
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
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
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
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            update_message_id("ghost", "grp", "msg-123")  # should not raise


# -- sweep_expired_questions ---------------------------------------------------


class TestSweepExpiredQuestions:
    @pytest.mark.asyncio
    async def test_keeps_expired_file_when_response_write_fails(self, ipc_dir: Path, settings):
        empty_group = ipc_dir / "empty-group"
        empty_group.mkdir()
        pending_dir = ipc_dir / "grp" / "pending_questions"
        pending_dir.mkdir(parents=True)
        pending_file = pending_dir / "req-failed.json"
        pending_file.write_text(
            json.dumps(
                {
                    "request_id": "req-failed",
                    "timestamp": (datetime.now(UTC) - timedelta(minutes=35)).isoformat(),
                }
            )
        )

        def fail_to_write(_group: str, _request_id: str, _error: str) -> None:
            raise OSError("response directory unavailable")

        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            expired = await sweep_expired_questions(fail_to_write)

        assert expired == []
        assert pending_file.exists()

    @pytest.mark.asyncio
    async def test_expires_old_pending(self, ipc_dir: Path, settings):
        with (
            patch(
                "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
            patch(
                "pynchy.host.container_manager.ipc.write._ipc_base_dir",
                settings.data_dir / "ipc",
            ),
        ):
            create_pending_question(
                request_id="req-old",
                source_group="grp",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[{"question": "Pick one"}],
            )

            # Backdate the file past the 30-minute timeout
            pending_file = ipc_dir / "grp" / "pending_questions" / "req-old.json"
            data = json.loads(pending_file.read_text())
            data["timestamp"] = (datetime.now(UTC) - timedelta(minutes=35)).isoformat()
            pending_file.write_text(json.dumps(data))

            expired = await sweep_expired_questions(_write_expiration_response)

        assert len(expired) == 1
        assert expired[0]["request_id"] == "req-old"
        assert not pending_file.exists()

        # Error response should have been written
        response_file = ipc_dir / "grp" / "responses" / "req-old.json"
        assert response_file.exists()
        response = json.loads(response_file.read_text())
        assert "error" in response
        assert "expired" in response["error"].lower()

    @pytest.mark.asyncio
    async def test_keeps_fresh_pending(self, ipc_dir: Path, settings):
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            create_pending_question(
                request_id="req-fresh",
                source_group="grp",
                chat_jid="slack:C1",
                channel_name="slack",
                session_id="sess-1",
                questions=[],
            )
            expired = await sweep_expired_questions(_write_expiration_response)

        assert len(expired) == 0
        assert (ipc_dir / "grp" / "pending_questions" / "req-fresh.json").exists()

    @pytest.mark.asyncio
    async def test_empty_ipc_dir_returns_empty(self, tmp_path: Path):
        """No ipc/ directory at all should return empty list."""
        s = make_settings(data_dir=tmp_path / "empty")
        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            s.data_dir / "ipc",
        ):
            expired = await sweep_expired_questions(_write_expiration_response)

        assert expired == []

    @pytest.mark.asyncio
    async def test_corrupt_json_handled_gracefully(self, ipc_dir: Path, settings):
        """Corrupt JSON files should be skipped without raising."""
        # Create a corrupt file directly
        pending_dir = ipc_dir / "grp" / "pending_questions"
        pending_dir.mkdir(parents=True)
        corrupt_file = pending_dir / "req-corrupt.json"
        corrupt_file.write_text("{not valid json")

        with patch(
            "pynchy.host.orchestrator.messaging.pending_questions._ipc_base_dir",
            settings.data_dir / "ipc",
        ):
            expired = await sweep_expired_questions(_write_expiration_response)

        assert expired == []
        # Corrupt file is left in place (not deleted, not crashed)
        assert corrupt_file.exists()
