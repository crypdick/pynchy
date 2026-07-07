"""Payload validation tests for the durable Obsidian learning IPC queue."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pynchy.host.learning.queue import LearningQueue
from tests.learning_queue_helpers import (
    base_dir as _base_dir,
)
from tests.learning_queue_helpers import (
    packet as _packet,
)
from tests.learning_queue_helpers import (
    read_json as _read_json,
)


@pytest.mark.parametrize("operation", ["complete", "fail"])
def test_claim_handle_rejects_current_payload_identity_mismatch(
    tmp_path: Path,
    operation: str,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60, max_attempts=2)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    payload = _read_json(claimed.path)
    payload["chat_jid"] = "slack:DIFFERENT"
    claimed.path.write_text(json.dumps(payload))

    with pytest.raises(RuntimeError, match="claim"):
        if operation == "complete":
            queue.complete(claimed)
        else:
            queue.fail(claimed, "stale failure")

    assert claimed.path.exists()
    assert not (_base_dir(tmp_path) / "done" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()


@pytest.mark.parametrize("operation", ["complete", "fail"])
def test_claim_handle_rejects_current_payload_filename_mismatch(
    tmp_path: Path,
    operation: str,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60, max_attempts=2)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    payload = _read_json(claimed.path)
    payload["job_id"] = "job-2"
    claimed.path.write_text(json.dumps(payload))

    with pytest.raises(RuntimeError, match="filename"):
        if operation == "complete":
            queue.complete(claimed)
        else:
            queue.fail(claimed, "stale failure")

    assert claimed.path.exists()
    assert not (_base_dir(tmp_path) / "done" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()


@pytest.mark.parametrize("operation", ["complete", "fail"])
def test_claim_handle_rejects_invalid_current_payload(
    tmp_path: Path,
    operation: str,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60, max_attempts=2)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    payload = _read_json(claimed.path)
    payload["messages"] = ["not a message object"]
    claimed.path.write_text(json.dumps(payload))

    with pytest.raises(RuntimeError, match="invalid payload"):
        if operation == "complete":
            queue.complete(claimed)
        else:
            queue.fail(claimed, "stale failure")

    assert claimed.path.exists()
    assert not (_base_dir(tmp_path) / "done" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()


def test_invalid_pending_json_moves_to_errors_with_compact_note(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path))
    bad_path = _base_dir(tmp_path) / "pending" / "bad.json"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("{not valid json" + ("x" * 1_000))

    assert queue.claim_next() is None

    error_path = _base_dir(tmp_path) / "errors" / "bad.json"
    assert error_path.exists()
    assert not bad_path.exists()
    note = _read_json(error_path)
    assert note["error"] == "invalid_json"
    assert note["filename"] == "bad.json"
    assert len(note["details"]) <= 240


@pytest.mark.parametrize("state", ["pending", "claimed", "done", "errors"])
def test_enqueue_rejects_duplicate_job_id_across_queue_states(
    tmp_path: Path,
    state: str,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path))
    existing_path = _base_dir(tmp_path) / state / "job-1.json"
    existing_path.write_text(json.dumps(asdict(_packet())))

    with pytest.raises(RuntimeError, match="already exists"):
        queue.enqueue(_packet())

    assert _read_json(existing_path) == asdict(_packet())


@pytest.mark.parametrize(
    "job_id",
    ["", "..", "../job-1", "nested/job-1", r"nested\job-1", "job..1"],
)
def test_enqueue_rejects_job_id_that_is_not_safe_filename_component(
    tmp_path: Path,
    job_id: str,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path))

    with pytest.raises(ValueError, match="job_id"):
        queue.enqueue(_packet(job_id=job_id))


def test_negative_attempts_payload_moves_to_errors(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path))
    bad_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    bad_path.write_text(json.dumps(asdict(replace(_packet(), attempts=-1))))

    assert queue.claim_next() is None

    error_path = _base_dir(tmp_path) / "errors" / "job-1.json"
    assert error_path.exists()
    assert not bad_path.exists()
    note = _read_json(error_path)
    assert note["error"] == "invalid_payload"
    assert "attempts" in note["details"]
