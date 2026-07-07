"""Basic tests for the durable Obsidian learning IPC queue."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.config.models import LearningConfig
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
