"""Guarded-action IDs, canonical payload hashes, and approval receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NewType

GuardedActionId = NewType("GuardedActionId", str)
RequestPayloadHash = NewType("RequestPayloadHash", str)
ApprovalReceiptToken = NewType("ApprovalReceiptToken", str)

_RECEIPT_FIELD = "_approval_receipt"


def guarded_action_id(request_id: str) -> GuardedActionId:
    """Parse the IPC request ID as the cross-layer guarded-action identity."""
    if not request_id:
        raise ValueError("A guarded action requires a non-empty request ID")
    return GuardedActionId(request_id)


def canonical_request_payload(data: dict[str, Any]) -> bytes:
    """Serialize the exact executable request, excluding only its receipt."""
    payload = {key: value for key, value in data.items() if key != _RECEIPT_FIELD}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def request_payload_hash(data: dict[str, Any]) -> RequestPayloadHash:
    """Return the SHA-256 identity of a canonical executable request."""
    return RequestPayloadHash(hashlib.sha256(canonical_request_payload(data)).hexdigest())


def payload_hash_matches(data: dict[str, Any], expected: str) -> bool:
    """Compare a request hash in constant time."""
    return hmac.compare_digest(str(request_payload_hash(data)), expected)


@dataclass(frozen=True)
class _ApprovalReceipt:
    token: ApprovalReceiptToken
    action_id: GuardedActionId
    workspace: str
    operation: str
    payload_hash: RequestPayloadHash


class ReceiptVerification(StrEnum):
    """Outcome of consuming a scoped, single-use approval receipt."""

    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


_receipts: dict[ApprovalReceiptToken, _ApprovalReceipt] = {}


def issue_approval_receipt(
    *,
    action_id: GuardedActionId,
    workspace: str,
    operation: str,
    request_data: dict[str, Any],
) -> ApprovalReceiptToken:
    """Issue a process-local exact-request receipt for one approved replay."""
    token = ApprovalReceiptToken(secrets.token_urlsafe(32))
    _receipts[token] = _ApprovalReceipt(
        token=token,
        action_id=action_id,
        workspace=workspace,
        operation=operation,
        payload_hash=request_payload_hash(request_data),
    )
    return token


def consume_approval_receipt(
    data: dict[str, Any],
    *,
    workspace: str,
    operation: str,
) -> ReceiptVerification:
    """Consume and verify an exact-request receipt; a mismatch burns the token."""
    raw_token = data.pop(_RECEIPT_FIELD, None)
    if raw_token is None:
        return ReceiptVerification.ABSENT
    if not isinstance(raw_token, str):
        return ReceiptVerification.INVALID

    receipt = _receipts.pop(ApprovalReceiptToken(raw_token), None)
    request_id = data.get("request_id")
    if receipt is None or not isinstance(request_id, str):
        return ReceiptVerification.INVALID

    identity_matches = hmac.compare_digest(str(receipt.action_id), request_id)
    workspace_matches = hmac.compare_digest(receipt.workspace, workspace)
    operation_matches = hmac.compare_digest(receipt.operation, operation)
    payload_matches = hmac.compare_digest(
        str(receipt.payload_hash),
        str(request_payload_hash(data)),
    )
    if identity_matches and workspace_matches and operation_matches and payload_matches:
        return ReceiptVerification.VALID
    return ReceiptVerification.INVALID


def revoke_approval_receipt(token: ApprovalReceiptToken) -> None:
    """Revoke an issued receipt that its destination did not consume."""
    _receipts.pop(token, None)


def clear_approval_receipts() -> None:
    """Clear process-local receipts during teardown and isolated tests."""
    _receipts.clear()
