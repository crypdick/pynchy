"""Terminal-state tests for the durable Obsidian learning IPC queue."""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pynchy.host.learning import queue as learning_queue
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
