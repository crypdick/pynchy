"""Tests for the durable Obsidian learning IPC queue."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
from pynchy.host.learning import queue as learning_queue
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
    for state in ("pending", "claiming", "claimed", "done", "errors"):
        assert (tmp_path / "data" / "ipc" / "learning" / state).is_dir()


def test_explicit_constructor_args_do_not_load_global_settings(tmp_path: Path):
    with patch(
        "pynchy.host.learning.queue.get_settings",
        side_effect=AssertionError("settings should not be loaded"),
    ):
        queue = LearningQueue(
            base_dir=_base_dir(tmp_path),
            lease_seconds=60,
            max_attempts=3,
        )

    assert queue.enqueue(_packet()).exists()


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


def test_complete_discards_claimed_duplicate_when_done_already_terminal(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    done_path = _base_dir(tmp_path) / "done" / "job-1.json"
    done_path.write_text(json.dumps({"sentinel": True}))

    with pytest.raises(RuntimeError, match="terminal state"):
        queue.complete(claimed)

    assert _read_json(done_path) == {"sentinel": True}
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()

    assert queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC)) == 0
    assert _read_json(done_path) == {"sentinel": True}
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()


def test_complete_discards_claimed_duplicate_when_done_appears_during_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    done_path = _base_dir(tmp_path) / "done" / "job-1.json"
    original_link = os.link

    def create_destination_before_link(src: Any, dst: Any, *args: Any, **kwargs: Any):
        if Path(dst) == done_path and not done_path.exists():
            done_path.write_text(json.dumps({"sentinel": True}))
        return original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", create_destination_before_link)

    with pytest.raises(RuntimeError, match="terminal state"):
        queue.complete(claimed)

    assert _read_json(done_path) == {"sentinel": True}
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()

    assert queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC)) == 0
    assert _read_json(done_path) == {"sentinel": True}
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()


def test_complete_discards_claimed_duplicate_when_errors_already_terminal(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60, max_attempts=2)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    error_path = _base_dir(tmp_path) / "errors" / "job-1.json"
    terminal_payload = _read_json(claimed.path)
    terminal_payload["terminal_marker"] = "errors"
    error_path.write_text(json.dumps(terminal_payload))

    with pytest.raises(RuntimeError, match="terminal state"):
        queue.complete(claimed)

    assert _read_json(error_path)["terminal_marker"] == "errors"
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "done" / "job-1.json").exists()

    assert queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC)) == 0
    assert _read_json(error_path)["terminal_marker"] == "errors"
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "done" / "job-1.json").exists()


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


def test_fail_discards_claimed_duplicate_when_done_already_terminal(
    tmp_path: Path,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=60, max_attempts=2)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    done_path = _base_dir(tmp_path) / "done" / "job-1.json"
    terminal_payload = _read_json(claimed.path)
    terminal_payload["terminal_marker"] = "done"
    done_path.write_text(json.dumps(terminal_payload))

    with pytest.raises(RuntimeError, match="terminal state"):
        queue.fail(claimed, "stale failure")

    assert _read_json(done_path)["terminal_marker"] == "done"
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()

    assert queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC)) == 0
    assert _read_json(done_path)["terminal_marker"] == "done"
    assert not claimed.path.exists()
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "errors" / "job-1.json").exists()


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


@pytest.mark.parametrize("terminal_state", ["done", "errors"])
def test_requeue_expired_exhausted_claim_terminal_collision_keeps_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal_state: str,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=1)
    queue.enqueue(_packet())
    exhausted_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert exhausted_claim is not None
    terminal_path = _base_dir(tmp_path) / terminal_state / "job-1.json"
    errors_path = _base_dir(tmp_path) / "errors" / "job-1.json"
    original_rename_no_clobber = learning_queue._rename_no_clobber

    def create_terminal_before_exhausted_move(source: Path, destination: Path) -> None:
        if source == exhausted_claim.path and destination == errors_path:
            terminal_path.write_text(json.dumps({"terminal_marker": terminal_state}))
        original_rename_no_clobber(source, destination)

    monkeypatch.setattr(
        learning_queue,
        "_rename_no_clobber",
        create_terminal_before_exhausted_move,
    )

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 0, 31, tzinfo=UTC))

    assert requeued == 0
    assert _read_json(terminal_path)["terminal_marker"] == terminal_state
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "claiming" / "job-1.json").exists()
    assert not exhausted_claim.path.exists()
    if terminal_state == "done":
        assert not errors_path.exists()


@pytest.mark.parametrize("terminal_state", ["done", "errors"])
def test_requeue_expired_discards_active_duplicate_when_terminal_state_exists(
    tmp_path: Path,
    terminal_state: str,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30, max_attempts=3)
    queue.enqueue(_packet())
    claimed = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert claimed is not None
    terminal_path = _base_dir(tmp_path) / terminal_state / "job-1.json"
    terminal_payload = _read_json(claimed.path)
    terminal_payload["terminal_marker"] = terminal_state
    terminal_path.write_text(json.dumps(terminal_payload))

    requeued = queue.requeue_expired(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))

    assert requeued == 0
    assert _read_json(terminal_path)["terminal_marker"] == terminal_state
    assert not (_base_dir(tmp_path) / "pending" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "claiming" / "job-1.json").exists()
    assert not (_base_dir(tmp_path) / "claimed" / "job-1.json").exists()


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


def test_complete_serializes_claim_verification_with_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    racing_queue = LearningQueue(base_dir=_base_dir(tmp_path), lease_seconds=30)
    queue.enqueue(_packet())
    first_claim = queue.claim_next(now=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))
    assert first_claim is not None
    original_current_claim_payload = queue._current_claim_payload
    race_finished = threading.Event()
    race_claim_ids: list[str] = []

    def current_payload_after_race(claimed):
        payload = original_current_claim_payload(claimed)

        def requeue_and_reclaim() -> None:
            try:
                requeued = racing_queue.requeue_expired(
                    now=datetime(2026, 7, 7, 12, 0, 31, tzinfo=UTC)
                )
                second_claim = racing_queue.claim_next(now=datetime(2026, 7, 7, 12, 1, tzinfo=UTC))
                if requeued == 1 and second_claim is not None:
                    race_claim_ids.append(second_claim.claim_id)
            finally:
                race_finished.set()

        thread = threading.Thread(target=requeue_and_reclaim)
        thread.start()
        race_finished.wait(timeout=0.2)
        return payload

    monkeypatch.setattr(queue, "_current_claim_payload", current_payload_after_race)

    done_path = queue.complete(first_claim)

    assert _read_json(done_path)["claim_id"] == first_claim.claim_id
    assert race_finished.wait(timeout=1)
    assert race_claim_ids == []


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
