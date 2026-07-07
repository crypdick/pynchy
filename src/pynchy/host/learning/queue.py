"""Durable filesystem queue for Obsidian learning packets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pynchy.config import get_settings
from pynchy.utils import write_json_atomic

_PENDING = "pending"
_CLAIMING = "claiming"
_CLAIMED = "claimed"
_DONE = "done"
_ERRORS = "errors"
_ERROR_DETAILS_MAX_CHARS = 200
_CLAIM_METADATA_KEYS = ("claim_id", "claimed_at", "lease_until")


class LearningQueueError(RuntimeError):
    """Raised when queue state changes would violate durable ownership."""


@dataclass(frozen=True)
class LearningPacket:
    job_id: str
    chat_jid: str
    group_folder: str
    profile: str
    created_at: str
    messages: list[dict[str, str]]
    final_answer: str | None
    tool_counts: dict[str, int]
    error_snippets: list[str]
    loaded_skills: list[str]
    provenance: dict[str, str]
    attempts: int = 0


@dataclass(frozen=True)
class ClaimedLearningPacket:
    packet: LearningPacket
    path: Path
    claim_id: str


class LearningQueue:
    def __init__(
        self,
        base_dir: Path | None = None,
        *,
        lease_seconds: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        settings = get_settings()
        self._base_dir = (
            base_dir if base_dir is not None else settings.data_dir / "ipc" / "learning"
        )
        self._lease_seconds = (
            lease_seconds if lease_seconds is not None else settings.learning.lease_seconds
        )
        self._max_attempts = (
            max_attempts if max_attempts is not None else settings.learning.max_attempts
        )
        if self._lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self._max_attempts <= 0:
            raise ValueError("max_attempts must be positive")

        for directory in (
            self._pending_dir,
            self._claiming_dir,
            self._claimed_dir,
            self._done_dir,
            self._errors_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def enqueue(self, packet: LearningPacket) -> Path:
        filename = self._ensure_job_id_available(packet.job_id)
        path = self._pending_dir / filename
        write_json_atomic(path, _packet_to_payload(packet), indent=2)
        return path

    def claim_next(self, *, now: datetime | None = None) -> ClaimedLearningPacket | None:
        claim_time = _coerce_utc(now)
        for pending_path in sorted(self._pending_dir.glob("*.json")):
            claiming_path = self._claiming_dir / pending_path.name
            try:
                _rename_no_clobber(pending_path, claiming_path)
            except FileNotFoundError:
                continue

            try:
                payload = _load_payload(claiming_path)
                packet = _packet_from_payload(payload)
                if _job_filename(packet.job_id) != pending_path.name:
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
            claimed_payload = _packet_to_payload(claimed_packet)
            claimed_payload["last_error"] = _string_metadata(payload, "last_error")
            claimed_payload["claim_id"] = claim_id
            claimed_payload["claimed_at"] = claim_time.isoformat()
            claimed_payload["lease_until"] = (
                claim_time + timedelta(seconds=self._lease_seconds)
            ).isoformat()
            write_json_atomic(claiming_path, claimed_payload, indent=2)
            claimed_path = self._claimed_dir / pending_path.name
            _rename_no_clobber(claiming_path, claimed_path)
            return ClaimedLearningPacket(
                packet=claimed_packet,
                path=claimed_path,
                claim_id=claim_id,
            )

        return None

    def complete(self, claimed: ClaimedLearningPacket) -> Path:
        self._current_claim_payload(claimed)
        done_path = self._done_dir / claimed.path.name
        _rename_no_clobber(claimed.path, done_path)
        return done_path

    def fail(self, claimed: ClaimedLearningPacket, reason: str) -> Path:
        current_payload = self._current_claim_payload(claimed)
        payload = _packet_to_payload(claimed.packet)
        payload["last_error"] = _cap_error(reason)

        if claimed.packet.attempts >= self._max_attempts:
            _copy_claim_metadata(current_payload, payload)
            write_json_atomic(claimed.path, payload, indent=2)
            destination = self._errors_dir / claimed.path.name
            _rename_no_clobber(claimed.path, destination)
            return destination

        return self._return_claimed_to_pending(claimed.path, payload)

    def requeue_expired(self, *, now: datetime | None = None) -> int:
        check_time = _coerce_utc(now)
        requeued = 0
        for claimed_path in sorted(self._claimed_dir.glob("*.json")):
            try:
                payload = _load_payload(claimed_path)
                packet = _packet_from_payload(payload)
                if _job_filename(packet.job_id) != claimed_path.name:
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

            if not _lease_is_expired(payload, check_time):
                continue

            if packet.attempts >= self._max_attempts:
                payload["last_error"] = _cap_error(
                    "lease expired after reaching max attempts "
                    f"({packet.attempts}/{self._max_attempts})"
                )
                write_json_atomic(claimed_path, payload, indent=2)
                _rename_no_clobber(claimed_path, self._errors_dir / claimed_path.name)
                continue

            self._return_claimed_to_pending(claimed_path, payload)
            requeued += 1

        return requeued

    def _ensure_job_id_available(self, job_id: str) -> str:
        filename = _job_filename(job_id)
        for directory in (
            self._pending_dir,
            self._claiming_dir,
            self._claimed_dir,
            self._done_dir,
            self._errors_dir,
        ):
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
            payload = _load_payload(claimed.path)
        except FileNotFoundError as exc:
            raise LearningQueueError("claimed file is missing") from exc
        except json.JSONDecodeError as exc:
            raise LearningQueueError("claimed file contains invalid JSON") from exc
        except ValueError as exc:
            raise LearningQueueError("claimed file contains an invalid payload") from exc

        if _string_metadata(payload, "claim_id") != claimed.claim_id:
            raise LearningQueueError("claim ownership mismatch")
        return payload

    def _return_claimed_to_pending(
        self,
        claimed_path: Path,
        payload: dict[str, Any],
    ) -> Path:
        claiming_path = self._claiming_dir / claimed_path.name
        _rename_no_clobber(claimed_path, claiming_path)
        write_json_atomic(claiming_path, _clear_claim_metadata(payload), indent=2)
        pending_path = self._pending_dir / claimed_path.name
        _rename_no_clobber(claiming_path, pending_path)
        return pending_path

    @property
    def _pending_dir(self) -> Path:
        return self._base_dir / _PENDING

    @property
    def _claiming_dir(self) -> Path:
        return self._base_dir / _CLAIMING

    @property
    def _claimed_dir(self) -> Path:
        return self._base_dir / _CLAIMED

    @property
    def _done_dir(self) -> Path:
        return self._base_dir / _DONE

    @property
    def _errors_dir(self) -> Path:
        return self._base_dir / _ERRORS

    def _move_bad_payload(self, path: Path, *, error: str, details: str) -> Path:
        error_payload = {
            "error": error,
            "filename": path.name,
            "details": _cap_error(details),
        }
        write_json_atomic(path, error_payload)
        error_path = self._errors_dir / path.name
        _rename_no_clobber(path, error_path)
        return error_path


def _job_filename(job_id: str) -> str:
    _validate_job_id(job_id)
    return f"{job_id}.json"


def _validate_job_id(job_id: str) -> None:
    if not job_id or job_id == "." or ".." in job_id or "/" in job_id or "\\" in job_id:
        raise ValueError("job_id must be a non-empty safe filename component")


def _coerce_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _packet_to_payload(packet: LearningPacket) -> dict[str, Any]:
    return asdict(packet)


def _load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("queue payload must be a JSON object")
    return cast(dict[str, Any], payload)


def _packet_from_payload(payload: Mapping[str, Any]) -> LearningPacket:
    return LearningPacket(
        job_id=_required_job_id(payload, "job_id"),
        chat_jid=_required_str(payload, "chat_jid"),
        group_folder=_required_str(payload, "group_folder"),
        profile=_required_str(payload, "profile"),
        created_at=_required_str(payload, "created_at"),
        messages=_required_message_list(payload, "messages"),
        final_answer=_optional_str(payload, "final_answer"),
        tool_counts=_required_int_dict(payload, "tool_counts"),
        error_snippets=_required_str_list(payload, "error_snippets"),
        loaded_skills=_required_str_list(payload, "loaded_skills"),
        provenance=_required_str_dict(payload, "provenance"),
        attempts=_optional_int(payload, "attempts", default=0),
    )


def _required_job_id(payload: Mapping[str, Any], key: str) -> str:
    value = _required_str(payload, key)
    _validate_job_id(value)
    return value


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_str(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload[key]
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"{key} must be a string or null")


def _required_str_list(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} items must be strings")
        result.append(item)
    return result


def _required_message_list(payload: Mapping[str, Any], key: str) -> list[dict[str, str]]:
    value = payload[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{key} items must be objects")
        result.append(_str_dict_from_mapping(item, f"{key} item"))
    return result


def _required_str_dict(payload: Mapping[str, Any], key: str) -> dict[str, str]:
    value = payload[key]
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return _str_dict_from_mapping(value, key)


def _required_int_dict(payload: Mapping[str, Any], key: str) -> dict[str, int]:
    value = payload[key]
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    result: dict[str, int] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise ValueError(f"{key} keys must be strings")
        if not isinstance(item_value, int) or isinstance(item_value, bool):
            raise ValueError(f"{key} values must be integers")
        result[item_key] = item_value
    return result


def _optional_int(payload: Mapping[str, Any], key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    if value < 0:
        raise ValueError(f"{key} must be non-negative")
    return value


def _str_dict_from_mapping(value: Mapping[Any, Any], field_name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if not isinstance(item_key, str):
            raise ValueError(f"{field_name} keys must be strings")
        if not isinstance(item_value, str):
            raise ValueError(f"{field_name} values must be strings")
        result[item_key] = item_value
    return result


def _string_metadata(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    return None


def _lease_is_expired(payload: Mapping[str, Any], now: datetime) -> bool:
    lease_value = payload.get("lease_until")
    if not isinstance(lease_value, str):
        return True
    try:
        lease_until = datetime.fromisoformat(lease_value)
    except ValueError:
        return True
    return _coerce_utc(lease_until) <= now


def _cap_error(value: str) -> str:
    if len(value) <= _ERROR_DETAILS_MAX_CHARS:
        return value
    return f"{value[: _ERROR_DETAILS_MAX_CHARS - 3]}..."


def _copy_claim_metadata(
    source: Mapping[str, Any],
    destination: dict[str, Any],
) -> None:
    for key in _CLAIM_METADATA_KEYS:
        if value := _string_metadata(source, key):
            destination[key] = value


def _clear_claim_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in _CLAIM_METADATA_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _rename_no_clobber(source: Path, destination: Path) -> None:
    if destination.exists():
        raise LearningQueueError(f"queue destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
