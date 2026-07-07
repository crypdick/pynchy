"""Durable filesystem queue for Obsidian learning packets."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pynchy.config import get_settings
from pynchy.host.learning import queue_codec as codec
from pynchy.host.learning import queue_recovery as recovery
from pynchy.host.learning.queue_fs import (
    QueueLayout,
    discard_duplicates_with_terminal_winner,
    ensure_state_dirs,
    transition_lock,
)
from pynchy.host.learning.queue_fs import (
    rename_no_clobber as _rename_no_clobber,
)
from pynchy.host.learning.queue_models import (
    ClaimedLearningPacket,
    LearningPacket,
    LearningQueueError,
)
from pynchy.utils import write_json_atomic


class LearningQueue:
    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        lease_seconds: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        if base_dir is None or lease_seconds is None or max_attempts is None:
            settings = get_settings()
            if base_dir is None:
                base_dir = settings.data_dir / "ipc" / "learning"
            if lease_seconds is None:
                lease_seconds = settings.learning.lease_seconds
            if max_attempts is None:
                max_attempts = settings.learning.max_attempts

        self._base_dir = base_dir
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        if self._lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self._max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        self._layout = QueueLayout(self._base_dir)
        ensure_state_dirs(self._layout)

    def enqueue(self, packet: LearningPacket) -> Path:
        with self._transition_lock():
            self._recover_terminal_duplicates()
            filename = self._ensure_job_id_available(packet.job_id)
            path = self._pending_dir / filename
            write_json_atomic(path, codec.packet_to_payload(packet), indent=2)
            return path

    def claim_next(self, *, now: datetime | None = None) -> ClaimedLearningPacket | None:
        claim_time = codec.coerce_utc(now)
        with self._transition_lock():
            self._recover_terminal_duplicates()
            self._recover_interrupted_claims()
            for pending_path in sorted(self._pending_dir.glob("*.json")):
                claiming_path = self._claiming_dir / pending_path.name
                try:
                    _rename_no_clobber(pending_path, claiming_path)
                except FileNotFoundError:
                    continue
                except LearningQueueError:
                    continue

                try:
                    payload = codec.load_payload(claiming_path)
                    packet = codec.packet_from_payload(payload)
                    if codec.job_filename(packet.job_id) != pending_path.name:
                        raise ValueError("job_id must match queue filename")
                except json.JSONDecodeError as exc:
                    self._move_bad_payload(
                        claiming_path,
                        error="invalid_json",
                        details=str(exc),
                    )
                    continue
                except (KeyError, TypeError, ValueError) as exc:
                    self._move_bad_payload(
                        claiming_path,
                        error="invalid_payload",
                        details=str(exc),
                    )
                    continue

                claimed_packet = replace(packet, attempts=packet.attempts + 1)
                claim_id = uuid4().hex
                claimed_payload = codec.packet_to_payload(claimed_packet)
                claimed_payload["last_error"] = codec.string_metadata(payload, "last_error")
                claimed_payload["claim_id"] = claim_id
                claimed_payload["claimed_at"] = claim_time.isoformat()
                claimed_payload["lease_until"] = (
                    claim_time + timedelta(seconds=self._lease_seconds)
                ).isoformat()
                claimed_payload[codec.CLAIMING_PREVIOUS_ATTEMPTS_KEY] = packet.attempts
                claimed_payload[codec.CLAIMING_TRANSITION_KEY] = (
                    codec.CLAIMING_TRANSITION_FRESH_CLAIM
                )
                write_json_atomic(claiming_path, claimed_payload, indent=2)
                claimed_path = self._claimed_dir / pending_path.name
                try:
                    _rename_no_clobber(claiming_path, claimed_path)
                except LearningQueueError as exc:
                    if claimed_path.exists():
                        claiming_path.unlink(missing_ok=True)
                        continue
                    self._move_bad_payload(
                        claiming_path,
                        error="claim_collision",
                        details=str(exc),
                    )
                    continue
                claimed_payload.pop(codec.CLAIMING_PREVIOUS_ATTEMPTS_KEY, None)
                claimed_payload.pop(codec.CLAIMING_TRANSITION_KEY, None)
                write_json_atomic(claimed_path, claimed_payload, indent=2)
                return ClaimedLearningPacket(
                    packet=claimed_packet,
                    path=claimed_path,
                    claim_id=claim_id,
                )

            return None

    def complete(self, claimed: ClaimedLearningPacket) -> Path:
        with self._transition_lock():
            self._raise_if_terminal_duplicate_exists(claimed.path.name)
            self._current_claim_payload(claimed)
            done_path = self._done_dir / claimed.path.name
            try:
                _rename_no_clobber(claimed.path, done_path)
            except LearningQueueError:
                self._raise_if_terminal_duplicate_exists(claimed.path.name)
                raise
            return done_path

    def fail(self, claimed: ClaimedLearningPacket, reason: str) -> Path:
        with self._transition_lock():
            self._raise_if_terminal_duplicate_exists(claimed.path.name)
            current_payload = self._current_claim_payload(claimed)
            payload = codec.packet_to_payload(claimed.packet)
            payload["last_error"] = codec.cap_error(reason)

            if claimed.packet.attempts >= self._max_attempts:
                codec.copy_claim_metadata(current_payload, payload)
                write_json_atomic(claimed.path, payload, indent=2)
                try:
                    return self._move_claimed_to_errors(claimed.path)
                except LearningQueueError:
                    self._raise_if_terminal_duplicate_exists(claimed.path.name)
                    raise

            return self._return_claimed_to_pending(claimed.path, payload)

    def requeue_expired(self, *, now: datetime | None = None) -> int:
        check_time = codec.coerce_utc(now)
        with self._transition_lock():
            self._recover_terminal_duplicates()
            requeued = self._recover_interrupted_claims()
            for claimed_path in sorted(self._claimed_dir.glob("*.json")):
                try:
                    payload = codec.load_payload(claimed_path)
                    packet = codec.packet_from_payload(payload)
                    if codec.job_filename(packet.job_id) != claimed_path.name:
                        raise ValueError("job_id must match queue filename")
                except json.JSONDecodeError as exc:
                    self._move_bad_payload(
                        claimed_path,
                        error="invalid_json",
                        details=str(exc),
                    )
                    continue
                except (KeyError, TypeError, ValueError) as exc:
                    self._move_bad_payload(
                        claimed_path,
                        error="invalid_payload",
                        details=str(exc),
                    )
                    continue

                if not codec.lease_is_expired(payload, check_time):
                    continue

                if packet.attempts >= self._max_attempts:
                    payload["last_error"] = codec.cap_error(
                        "lease expired after reaching max attempts "
                        f"({packet.attempts}/{self._max_attempts})"
                    )
                    write_json_atomic(claimed_path, payload, indent=2)
                    self._move_claimed_to_errors(claimed_path)
                    continue

                self._return_claimed_to_pending(claimed_path, payload)
                requeued += 1

            return requeued

    def _ensure_job_id_available(self, job_id: str) -> str:
        filename = codec.job_filename(job_id)
        for directory in self._layout.state_dirs:
            existing_path = directory / filename
            if existing_path.exists():
                raise LearningQueueError(
                    f"job_id {job_id!r} already exists in queue state {directory.name}"
                )
        return filename

    def _current_claim_payload(
        self,
        claimed: ClaimedLearningPacket,
    ) -> dict[str, Any]:
        try:
            payload = codec.load_payload(claimed.path)
            codec.validate_claim_payload(payload, filename=claimed.path.name, claimed=claimed)
        except FileNotFoundError as exc:
            raise LearningQueueError("claimed file is missing") from exc
        except json.JSONDecodeError as exc:
            raise LearningQueueError("claimed file contains invalid JSON") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise LearningQueueError("claimed file contains an invalid payload") from exc
        return payload

    def _raise_if_terminal_duplicate_exists(self, filename: str) -> None:
        terminal_path = self._terminal_duplicate_path(filename)
        if terminal_path is None:
            return
        self._recover_terminal_duplicates()
        raise LearningQueueError(
            f"terminal state already exists for claimed job: {terminal_path.parent.name}/{filename}"
        )

    def _terminal_duplicate_path(self, filename: str) -> Path | None:
        for terminal_dir in self._layout.terminal_dirs:
            terminal_path = terminal_dir / filename
            if terminal_path.exists():
                return terminal_path
        return None

    def _return_claimed_to_pending(
        self,
        claimed_path: Path,
        payload: dict[str, Any],
    ) -> Path:
        staged_payload = codec.clear_claim_metadata(payload)
        staged_payload[codec.CLAIMING_TRANSITION_KEY] = codec.CLAIMING_TRANSITION_RETURN_TO_PENDING
        write_json_atomic(claimed_path, staged_payload, indent=2)
        claiming_path = self._claiming_dir / claimed_path.name
        _rename_no_clobber(claimed_path, claiming_path)
        pending_payload = codec.clear_claim_metadata(staged_payload)
        write_json_atomic(claiming_path, pending_payload, indent=2)
        pending_path = self._pending_dir / claimed_path.name
        try:
            _rename_no_clobber(claiming_path, pending_path)
        except LearningQueueError:
            if pending_path.exists():
                claiming_path.unlink(missing_ok=True)
                return pending_path
            raise
        return pending_path

    def _recover_interrupted_claims(self) -> int:
        recovered = 0
        for claiming_path in sorted(self._claiming_dir.glob("*.json")):
            pending_path = self._pending_dir / claiming_path.name
            claimed_path = self._claimed_dir / claiming_path.name
            if pending_path.exists() or claimed_path.exists():
                # A crash during the hard-link move can leave both source and
                # destination names. The non-claiming active state is already
                # the canonical job copy.
                claiming_path.unlink(missing_ok=True)
                continue

            try:
                payload = codec.load_payload(claiming_path)
                packet = codec.packet_from_payload(payload)
                if codec.job_filename(packet.job_id) != claiming_path.name:
                    raise ValueError("job_id must match queue filename")
            except json.JSONDecodeError as exc:
                self._move_bad_payload(
                    claiming_path,
                    error="invalid_json",
                    details=str(exc),
                )
                continue
            except (KeyError, TypeError, ValueError) as exc:
                self._move_bad_payload(
                    claiming_path,
                    error="invalid_payload",
                    details=str(exc),
                )
                continue

            pending_payload = codec.clear_claim_metadata(payload)
            pending_payload["attempts"] = recovery.recovered_attempts(payload, packet.attempts)
            write_json_atomic(claiming_path, pending_payload, indent=2)
            try:
                _rename_no_clobber(claiming_path, pending_path)
            except LearningQueueError as exc:
                self._move_bad_payload(
                    claiming_path,
                    error="claim_collision",
                    details=str(exc),
                )
                continue
            recovered += 1

        recovered += self._recover_claimed_transition_markers()
        return recovered

    def _recover_claimed_transition_markers(self) -> int:
        recovered = 0
        for claimed_path in sorted(self._claimed_dir.glob("*.json")):
            pending_path = self._pending_dir / claimed_path.name
            transition_payload = self._pending_payload_from_claimed_transition(claimed_path)
            if transition_payload is None:
                continue
            if pending_path.exists():
                claimed_path.unlink(missing_ok=True)
                continue

            write_json_atomic(claimed_path, transition_payload, indent=2)
            try:
                _rename_no_clobber(claimed_path, pending_path)
            except LearningQueueError as exc:
                self._move_bad_payload(
                    claimed_path,
                    error="claim_collision",
                    details=str(exc),
                )
                continue
            recovered += 1
        return recovered

    def _pending_payload_from_claimed_transition(
        self,
        claimed_path: Path,
    ) -> dict[str, Any] | None:
        try:
            return recovery.pending_payload_from_claimed_transition(claimed_path)
        except json.JSONDecodeError as exc:
            self._move_bad_payload(
                claimed_path,
                error="invalid_json",
                details=str(exc),
            )
            return None
        except (KeyError, TypeError, ValueError) as exc:
            self._move_bad_payload(
                claimed_path,
                error="invalid_payload",
                details=str(exc),
            )
            return None

    def _move_claimed_to_errors(self, claimed_path: Path) -> Path:
        destination = self._errors_dir / claimed_path.name
        try:
            _rename_no_clobber(claimed_path, destination)
        except LearningQueueError:
            if self._terminal_duplicate_path(claimed_path.name) is None:
                raise
            self._recover_terminal_duplicates()
            return destination
        self._recover_terminal_duplicates()
        return destination

    def _recover_terminal_duplicates(self) -> int:
        return discard_duplicates_with_terminal_winner(self._layout)

    def _transition_lock(self):
        return transition_lock(self._base_dir)

    @property
    def _pending_dir(self) -> Path:
        return self._layout.pending_dir

    @property
    def _claiming_dir(self) -> Path:
        return self._layout.claiming_dir

    @property
    def _claimed_dir(self) -> Path:
        return self._layout.claimed_dir

    @property
    def _done_dir(self) -> Path:
        return self._layout.done_dir

    @property
    def _errors_dir(self) -> Path:
        return self._layout.errors_dir

    def _move_bad_payload(self, path: Path, *, error: str, details: str) -> Path:
        error_payload = {
            "error": error,
            "filename": path.name,
            "details": codec.cap_error(details),
        }
        write_json_atomic(path, error_payload)
        error_path = self._errors_dir / path.name
        _rename_no_clobber(path, error_path)
        return error_path
