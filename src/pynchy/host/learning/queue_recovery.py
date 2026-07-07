"""Recovery helpers for staged learning queue transitions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pynchy.host.learning import queue_codec as codec

REPLAYABLE_CLAIMED_TRANSITIONS = frozenset(
    {
        codec.CLAIMING_TRANSITION_FRESH_CLAIM,
        codec.CLAIMING_TRANSITION_RETURN_TO_PENDING,
    }
)


def pending_payload_from_claimed_transition(path: Path) -> dict[str, Any] | None:
    payload = codec.load_payload(path)
    transition = codec.string_metadata(payload, codec.CLAIMING_TRANSITION_KEY)
    if transition not in REPLAYABLE_CLAIMED_TRANSITIONS:
        return None

    packet = codec.packet_from_payload(payload)
    if codec.job_filename(packet.job_id) != path.name:
        raise ValueError("job_id must match queue filename")

    pending_payload = codec.clear_claim_metadata(payload)
    pending_payload["attempts"] = recovered_attempts(payload, packet.attempts)
    return pending_payload


def recovered_attempts(payload: Mapping[str, Any], attempts: int) -> int:
    transition = codec.string_metadata(payload, codec.CLAIMING_TRANSITION_KEY)
    if transition == codec.CLAIMING_TRANSITION_RETURN_TO_PENDING:
        return attempts

    previous_attempts = payload.get(codec.CLAIMING_PREVIOUS_ATTEMPTS_KEY)
    if (
        isinstance(previous_attempts, int)
        and not isinstance(previous_attempts, bool)
        and 0 <= previous_attempts <= attempts
    ):
        return previous_attempts
    if transition == codec.CLAIMING_TRANSITION_FRESH_CLAIM:
        return max(attempts - 1, 0)
    if has_claim_metadata(payload):
        return max(attempts - 1, 0)
    return attempts


def has_claim_metadata(payload: Mapping[str, Any]) -> bool:
    return any(
        codec.string_metadata(payload, key) is not None for key in codec.CLAIM_STRING_METADATA_KEYS
    )
