"""Payload codec and validation helpers for the Obsidian learning queue."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pynchy.host.learning.queue_models import (
    ClaimedLearningPacket,
    LearningPacket,
    LearningQueueError,
)

ERROR_DETAILS_MAX_CHARS = 200
CLAIMING_PREVIOUS_ATTEMPTS_KEY = "_claiming_previous_attempts"
CLAIMING_TRANSITION_KEY = "_claiming_transition"
CLAIMING_TRANSITION_FRESH_CLAIM = "fresh_claim"
CLAIMING_TRANSITION_RETURN_TO_PENDING = "return_to_pending"
CLAIM_METADATA_KEYS = (
    "claim_id",
    "claimed_at",
    "lease_until",
    CLAIMING_PREVIOUS_ATTEMPTS_KEY,
    CLAIMING_TRANSITION_KEY,
)


def job_filename(job_id: str) -> str:
    validate_job_id(job_id)
    return f"{job_id}.json"


def validate_job_id(job_id: str) -> None:
    if not job_id or job_id == "." or ".." in job_id or "/" in job_id or "\\" in job_id:
        raise ValueError("job_id must be a non-empty safe filename component")


def coerce_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def packet_to_payload(packet: LearningPacket) -> dict[str, Any]:
    return asdict(packet)


def load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("queue payload must be a JSON object")
    return cast(dict[str, Any], payload)


def packet_from_payload(payload: Mapping[str, Any]) -> LearningPacket:
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


def validate_claim_payload(
    payload: Mapping[str, Any],
    *,
    filename: str,
    claimed: ClaimedLearningPacket,
) -> None:
    packet = packet_from_payload(payload)
    if job_filename(packet.job_id) != filename:
        raise LearningQueueError("claimed file job_id must match queue filename")
    if packet != claimed.packet:
        raise LearningQueueError("claim packet identity mismatch")
    if string_metadata(payload, "claim_id") != claimed.claim_id:
        raise LearningQueueError("claim ownership mismatch")


def string_metadata(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str):
        return value
    return None


def lease_is_expired(payload: Mapping[str, Any], now: datetime) -> bool:
    lease_value = payload.get("lease_until")
    if not isinstance(lease_value, str):
        return True
    try:
        lease_until = datetime.fromisoformat(lease_value)
    except ValueError:
        return True
    return coerce_utc(lease_until) <= now


def cap_error(value: str) -> str:
    if len(value) <= ERROR_DETAILS_MAX_CHARS:
        return value
    return f"{value[: ERROR_DETAILS_MAX_CHARS - 3]}..."


def copy_claim_metadata(
    source: Mapping[str, Any],
    destination: dict[str, Any],
) -> None:
    for key in CLAIM_METADATA_KEYS:
        if value := string_metadata(source, key):
            destination[key] = value


def clear_claim_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in CLAIM_METADATA_KEYS:
        cleaned.pop(key, None)
    return cleaned


def _required_job_id(payload: Mapping[str, Any], key: str) -> str:
    value = _required_str(payload, key)
    validate_job_id(value)
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
