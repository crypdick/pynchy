"""Recovery tests for the durable Obsidian learning IPC queue."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from pynchy.host.learning import queue as learning_queue
from pynchy.host.learning import queue_codec as codec
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


def test_recovers_fresh_claim_transition_stranded_in_claimed_without_exhausting(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=1)
    claim_time = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
    claimed_path = _base_dir(tmp_path) / "claimed" / "job-1.json"
    claimed_payload = asdict(replace(_packet(), attempts=1))
    claimed_payload["last_error"] = None
    claimed_payload["claim_id"] = "claim-before-crash"
    claimed_payload["claimed_at"] = claim_time.isoformat()
    claimed_payload["lease_until"] = (claim_time + timedelta(seconds=30)).isoformat()
    claimed_payload[codec.CLAIMING_PREVIOUS_ATTEMPTS_KEY] = 0
    claimed_payload[codec.CLAIMING_TRANSITION_KEY] = codec.CLAIMING_TRANSITION_FRESH_CLAIM
    claimed_path.write_text(json.dumps(claimed_payload))

    recovered = queue.requeue_expired(now=claim_time + timedelta(seconds=31))

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert recovered == 1
    assert pending_path.exists()
    assert not claimed_path.exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()
    pending_payload = _read_json(pending_path)
    assert pending_payload["attempts"] == 0
    assert "claim_id" not in pending_payload
    assert "claimed_at" not in pending_payload
    assert "lease_until" not in pending_payload
    assert codec.CLAIMING_PREVIOUS_ATTEMPTS_KEY not in pending_payload
    assert codec.CLAIMING_TRANSITION_KEY not in pending_payload


def test_recovers_return_to_pending_transition_stranded_in_claimed(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=1)
    claimed_path = _base_dir(tmp_path) / "claimed" / "job-1.json"
    claimed_payload = asdict(replace(_packet(), attempts=1))
    claimed_payload["last_error"] = "first failure"
    claimed_payload[codec.CLAIMING_TRANSITION_KEY] = codec.CLAIMING_TRANSITION_RETURN_TO_PENDING
    claimed_path.write_text(json.dumps(claimed_payload))

    recovered = queue.requeue_expired(
        now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC),
    )

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert recovered == 1
    assert pending_path.exists()
    assert not claimed_path.exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()
    pending_payload = _read_json(pending_path)
    assert pending_payload["attempts"] == 1
    assert pending_payload["last_error"] == "first failure"
    assert codec.CLAIMING_TRANSITION_KEY not in pending_payload


def test_requeue_expired_returns_claimed_jobs_to_pending(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, 31, tzinfo=UTC))

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert requeued == 1
    assert pending_path.exists()
    assert not claimed.path.exists()
    payload = _read_json(pending_path)
    assert payload["attempts"] == 1
    assert "claim_id" not in payload
    assert "claimed_at" not in payload
    assert "lease_until" not in payload


def test_requeue_expired_recovers_job_stranded_in_claiming_after_failed_claim(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet())
    original_write_json_atomic = learning_queue.write_json_atomic

    def fail_claim_metadata_write(path: Path, payload: object, **kwargs: object) -> None:
        if path.parent.name == "claiming":
            raise OSError("simulated crash between claiming and claimed")
        original_write_json_atomic(path, payload, **kwargs)

    with (
        patch(
            "pynchy.host.learning.queue.write_json_atomic",
            side_effect=fail_claim_metadata_write,
        ),
        pytest.raises(OSError, match="simulated crash"),
    ):
        queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))

    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    assert claiming_path.exists()

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert requeued == 1
    assert pending_path.exists()
    assert not claiming_path.exists()


def test_requeue_expired_rolls_back_fresh_claim_interrupted_after_metadata_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=5)
    packet = replace(_packet(), attempts=2)
    queue.enqueue(packet)
    original_rename_no_clobber = learning_queue._rename_no_clobber

    def crash_after_fresh_claim_metadata(source: Path, destination: Path) -> None:
        if source.parent.name == "claiming" and destination.parent.name == "claimed":
            raise OSError("simulated crash before claim finalization")
        original_rename_no_clobber(source, destination)

    monkeypatch.setattr(
        learning_queue,
        "_rename_no_clobber",
        crash_after_fresh_claim_metadata,
    )

    with pytest.raises(OSError, match="simulated crash"):
        queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))

    monkeypatch.setattr(learning_queue, "_rename_no_clobber", original_rename_no_clobber)
    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    assert _read_json(claiming_path)["attempts"] == 3

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert requeued == 1
    assert pending_path.exists()
    assert not claiming_path.exists()
    pending_payload = _read_json(pending_path)
    assert pending_payload["attempts"] == 2
    assert "claim_id" not in pending_payload
    assert "claimed_at" not in pending_payload
    assert "lease_until" not in pending_payload


def test_requeue_expired_rewinds_attempt_from_interrupted_claim_with_metadata(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    interrupted_payload = asdict(replace(_packet(), attempts=1))
    interrupted_payload["claim_id"] = "interrupted-claim"
    interrupted_payload["claimed_at"] = datetime(2026, 7, 7, 12, 0, tzinfo=UTC).isoformat()
    interrupted_payload["lease_until"] = datetime(2026, 7, 7, 12, 0, 30, tzinfo=UTC).isoformat()
    claiming_path.write_text(json.dumps(interrupted_payload))

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert requeued == 1
    assert pending_path.exists()
    assert not claiming_path.exists()
    pending_payload = _read_json(pending_path)
    assert pending_payload["attempts"] == 0
    assert "claim_id" not in pending_payload
    assert "claimed_at" not in pending_payload
    assert "lease_until" not in pending_payload


def test_fail_recovery_preserves_attempt_from_interrupted_return_to_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=3)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    original_rename_no_clobber = learning_queue._rename_no_clobber

    def crash_after_claimed_moves_to_staging(source: Path, destination: Path) -> None:
        original_rename_no_clobber(source, destination)
        if source.parent.name == "claimed" and destination.parent.name == "claiming":
            raise OSError("simulated crash before pending rewrite")

    monkeypatch.setattr(
        learning_queue,
        "_rename_no_clobber",
        crash_after_claimed_moves_to_staging,
    )

    with pytest.raises(OSError, match="simulated crash"):
        queue.fail(claimed, "first failure")

    monkeypatch.setattr(learning_queue, "_rename_no_clobber", original_rename_no_clobber)
    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    assert claiming_path.exists()

    recovered = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert recovered == 1
    assert pending_path.exists()
    assert not claiming_path.exists()
    pending_payload = _read_json(pending_path)
    assert pending_payload["attempts"] == 1
    assert pending_payload["last_error"] == "first failure"
    assert "claim_id" not in pending_payload
    assert "claimed_at" not in pending_payload
    assert "lease_until" not in pending_payload


def test_requeue_recovery_preserves_attempt_from_interrupted_expired_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=3)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    original_rename_no_clobber = learning_queue._rename_no_clobber

    def crash_after_claimed_moves_to_staging(source: Path, destination: Path) -> None:
        original_rename_no_clobber(source, destination)
        if source.parent.name == "claimed" and destination.parent.name == "claiming":
            raise OSError("simulated crash before pending rewrite")

    monkeypatch.setattr(
        learning_queue,
        "_rename_no_clobber",
        crash_after_claimed_moves_to_staging,
    )

    with pytest.raises(OSError, match="simulated crash"):
        queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, 31, tzinfo=UTC))

    monkeypatch.setattr(learning_queue, "_rename_no_clobber", original_rename_no_clobber)
    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    assert claiming_path.exists()

    recovered = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    pending_path = _base_dir(tmp_path) / "pending" / "job-1.json"
    assert recovered == 1
    assert pending_path.exists()
    assert not claiming_path.exists()
    pending_payload = _read_json(pending_path)
    assert pending_payload["attempts"] == 1
    assert "claim_id" not in pending_payload
    assert "claimed_at" not in pending_payload
    assert "lease_until" not in pending_payload


def test_requeue_expired_removes_claiming_duplicate_and_keeps_pending(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    pending_path = queue.enqueue(_packet())
    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    claiming_path.write_text(pending_path.read_text())

    first_pass = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))

    error_path = _base_dir(tmp_path) / "errors" / "job-1.json"
    assert first_pass == 0
    assert pending_path.exists()
    assert not claiming_path.exists()
    assert not error_path.exists()

    second_pass = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    assert second_pass == 0
    assert pending_path.exists()
    assert not claiming_path.exists()
    assert not error_path.exists()


def test_requeue_expired_removes_claiming_duplicate_and_keeps_claimed(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    claiming_path.write_text(claimed.path.read_text())

    first_pass = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, 15, tzinfo=UTC))

    error_path = _base_dir(tmp_path) / "errors" / "job-1.json"
    assert first_pass == 0
    assert claimed.path.exists()
    assert not claiming_path.exists()
    assert not error_path.exists()

    second_pass = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, 20, tzinfo=UTC))

    assert second_pass == 0
    assert claimed.path.exists()
    assert not claiming_path.exists()
    assert not error_path.exists()


def test_claim_next_recovers_interrupted_claiming_job_before_scanning_pending(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    claiming_path.write_text(json.dumps(asdict(_packet())))

    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    assert claimed is not None
    assert claimed.packet == replace(_packet(), attempts=1)
    assert claimed.path == _base_dir(tmp_path) / "claimed" / "job-1.json"
    assert not claiming_path.exists()


def test_claim_next_treats_claiming_transition_collision_as_lost_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet("job-1"))
    queue.enqueue(_packet("job-2"))
    original_rename_no_clobber = learning_queue._rename_no_clobber

    def lose_first_claiming_race(source: Path, destination: Path) -> None:
        if source.name == "job-1.json" and destination.parent.name == "claiming":
            raise learning_queue.LearningQueueError("queue destination already exists")
        original_rename_no_clobber(source, destination)

    monkeypatch.setattr(learning_queue, "_rename_no_clobber", lose_first_claiming_race)

    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))

    assert claimed is not None
    assert claimed.packet.job_id == "job-2"
    assert (_base_dir(tmp_path) / "pending" / "job-1.json").exists()


def test_claim_finalization_collision_keeps_active_claimed_without_errors(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    pending_path = queue.enqueue(_packet())
    claimed_path = _base_dir(tmp_path) / "claimed" / "job-1.json"
    claimed_payload = asdict(replace(_packet(), attempts=1))
    claimed_payload["last_error"] = None
    claimed_payload["claim_id"] = "existing-claim"
    claimed_payload["claimed_at"] = datetime(2026, 7, 7, 12, 0, tzinfo=UTC).isoformat()
    claimed_payload["lease_until"] = datetime(2026, 7, 7, 12, 5, tzinfo=UTC).isoformat()
    claimed_path.write_text(json.dumps(claimed_payload))

    assert pending_path.exists()
    assert queue.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC)) is None

    claiming_path = _base_dir(tmp_path) / "claiming" / "job-1.json"
    error_path = _base_dir(tmp_path) / "errors" / "job-1.json"
    assert claimed_path.exists()
    assert not pending_path.exists()
    assert not claiming_path.exists()
    assert not error_path.exists()

    second_pass = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 2, tzinfo=UTC))

    assert second_pass == 0
    assert _read_json(claimed_path)["claim_id"] == "existing-claim"
    assert not pending_path.exists()
    assert not claiming_path.exists()
    assert not error_path.exists()
