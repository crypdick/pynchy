"""Tests for the durable Obsidian learning IPC queue."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
from pynchy.host.learning.queue import LearningPacket, LearningQueue


def _packet(job_id: str = "job-1") -> LearningPacket:
    return LearningPacket(
        job_id=job_id,
        chat_jid="slack:C123",
        group_folder="shopping",
        profile="default",
        created_at="2026-07-07T10:00:00+00:00",
        messages=[{"role": "user", "content": "remember the milk"}],
        final_answer="Added milk to the list.",
        tool_counts={"shell": 1},
        error_snippets=["temporary model error"],
        loaded_skills=["shopping-list"],
        provenance={"run_id": "run-123"},
    )


def _base_dir(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ipc" / "learning"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_enqueue_writes_packet_to_default_pending_dir(tmp_path: Path):
    settings = make_settings(
        data_dir=tmp_path / "data",
        learning=LearningConfig(lease_seconds=45, max_attempts=4),
    )

    with patch("pynchy.host.learning.queue.get_settings", return_value=settings):
        path = LearningQueue().enqueue(_packet())

    assert path == tmp_path / "data" / "ipc" / "learning" / "pending" / "job-1.json"
    assert _read_json(path) == asdict(_packet())
    for state in ("pending", "claimed", "done", "errors"):
        assert (tmp_path / "data" / "ipc" / "learning" / state).is_dir()


def test_claim_next_moves_one_pending_job_to_claimed_and_increments_attempts(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60)
    queue.enqueue(_packet())
    now = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)

    claimed = queue.claim_next(now=now)

    assert claimed is not None
    assert claimed.path == _base_dir(tmp_path) / "claimed" / "job-1.json"
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert claimed.packet == replace(_packet(), attempts=1)
    payload = _read_json(claimed.path)
    assert payload["attempts"] == 1
    assert payload["claim_id"] == claimed.claim_id
    assert payload["claimed_at"] == now.isoformat()
    assert payload["lease_until"] == (now + timedelta(seconds=60)).isoformat()
    assert payload["last_error"] is None


def test_second_queue_instance_cannot_claim_job_after_first_claim(tmp_path: Path):
    first = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60)
    second = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60)
    first.enqueue(_packet())

    claimed = first.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))

    assert claimed is not None
    assert second.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC)) is None
    assert list((_base_dir(tmp_path) / "pending").glob("*.json")) == []
    assert [path.name for path in (_base_dir(tmp_path) / "claimed").glob("*.json")] == [
        "job-1.json"
    ]


def test_complete_moves_claimed_job_to_done(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None

    done_path = queue.complete(claimed)

    assert done_path == _base_dir(tmp_path) / "done" / "job-1.json"
    assert done_path.exists()
    assert not claimed.path.exists()
    assert _read_json(done_path)["attempts"] == 1


def test_stale_claim_cannot_complete_reclaimed_job(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet())
    first_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert first_claim is not None
    assert queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, 31, tzinfo=UTC)) == 1
    second_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))
    assert second_claim is not None

    with pytest.raises(RuntimeError, match="claim"):
        queue.complete(first_claim)

    assert second_claim.path.exists()
    assert not (_base_dir(tmp_path) / "done" / "job-1.json").exists()
    assert _read_json(second_claim.path)["claim_id"] == second_claim.claim_id


def test_stale_claim_cannot_fail_reclaimed_job(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet())
    first_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert first_claim is not None
    assert queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, 31, tzinfo=UTC)) == 1
    second_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))
    assert second_claim is not None

    with pytest.raises(RuntimeError, match="claim"):
        queue.fail(first_claim, "stale failure")

    assert second_claim.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()
    assert _read_json(second_claim.path)["claim_id"] == second_claim.claim_id


def test_complete_rejects_done_collision_without_overwriting_claimed_job(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    done_path = _base_dir(tmp_path) / "done" / "job-1.json"
    done_path.write_text(json.dumps({"sentinel": True}))

    with pytest.raises(RuntimeError, match="destination"):
        queue.complete(claimed)

    assert _read_json(done_path) == {"sentinel": True}
    assert claimed.path.exists()


def test_fail_requeues_until_max_attempts_then_moves_to_errors(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60, max_attempts=2)
    queue.enqueue(_packet())

    first_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert first_claim is not None
    pending_path = queue.fail(first_claim, "first failure")

    assert pending_path == _base_dir(tmp_path) / "pending" / "job-1.json"
    assert pending_path.exists()
    first_payload = _read_json(pending_path)
    assert first_payload["attempts"] == 1
    assert first_payload["last_error"] == "first failure"
    assert "claim_id" not in first_payload
    assert "claimed_at" not in first_payload
    assert "lease_until" not in first_payload

    second_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))
    assert second_claim is not None
    error_path = queue.fail(second_claim, "second failure")

    assert error_path == _base_dir(tmp_path) / "errors" / "job-1.json"
    assert error_path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "claimed" / "job-1.json").exists()
    error_payload = _read_json(error_path)
    assert error_payload["attempts"] == 2
    assert error_payload["last_error"] == "second failure"


def test_fail_caps_long_reason_before_requeueing(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60, max_attempts=2)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None

    pending_path = queue.fail(claimed, "x" * 500)

    payload = _read_json(pending_path)
    assert len(payload["last_error"]) == 200
    assert payload["last_error"].endswith("...")


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


def test_requeue_expired_moves_exhausted_claim_to_errors(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=2)
    queue.enqueue(_packet())
    first_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert first_claim is not None
    assert queue.fail(first_claim, "first failure").parent.name == "pending"
    exhausted_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))
    assert exhausted_claim is not None

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, 31, tzinfo=UTC))

    error_path = _base_dir(tmp_path) / "errors" / "job-1.json"
    assert requeued == 0
    assert error_path.exists()
    assert not exhausted_claim.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    error_payload = _read_json(error_path)
    assert error_payload["attempts"] == 2
    assert "max attempts" in error_payload["last_error"]


def test_requeue_expired_treats_missing_lease_until_as_expired(tmp_path: Path):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    payload = _read_json(claimed.path)
    del payload["lease_until"]
    claimed.path.write_text(json.dumps(payload))

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))

    assert requeued == 1
    assert (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not claimed.path.exists()


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
