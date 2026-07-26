"""Exact Linear response and callback evidence for generic effect correlation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pynchy.plugins.integrations.linear_errors import LinearError
from pynchy.webhook_effects import WebhookEffectEvidence, WebhookEffectScope

_COMMENT_EVIDENCE_MISSING = "Linear commentCreate response lacks self-echo evidence"
_COMMENT_ISSUE_MISMATCH = "Linear commentCreate response belongs to another issue"
_ISSUE_STATE_EVIDENCE_MISSING = "Linear issueUpdate response lacks self-echo evidence"
_ISSUE_STATE_ISSUE_MISMATCH = "Linear issueUpdate response belongs to another issue"
_ISSUE_STATE_TARGET_MISMATCH = "Linear issueUpdate response has another state"


def _fingerprint(kind: str, **fields: str) -> str:
    payload = json.dumps({"kind": kind, **fields}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def comment_mutation_intent(issue_id: str, body: str) -> str:
    """Fingerprint the request fields needed to investigate an unknown comment."""
    return _fingerprint(
        "linear-comment-intent",
        body_sha256=hashlib.sha256(body.encode()).hexdigest(),
        issue_id=issue_id,
    )


def issue_state_mutation_intent(
    issue_id: str,
    state_id: str,
    *,
    description: str | None = None,
) -> str:
    """Fingerprint the requested issue state and optional plan description."""
    fields = {"issue_id": issue_id, "state_id": state_id}
    if description is not None:
        fields["description_sha256"] = hashlib.sha256(description.encode()).hexdigest()
    return _fingerprint("linear-issue-state-intent", **fields)


def comment_webhook_evidence(
    account_name: str,
    *,
    comment_id: str,
    issue_id: str,
    revision: str,
    action: str = "create",
) -> WebhookEffectEvidence:
    """Build exact evidence for one Linear comment callback."""
    return WebhookEffectEvidence(
        scope=WebhookEffectScope(
            provider="linear",
            account=account_name,
            event_type="Comment",
            event_action=action,
            subject_id=issue_id,
        ),
        fingerprint=_fingerprint(
            "linear-comment",
            action=action,
            comment_id=comment_id,
            issue_id=issue_id,
            revision=revision,
        ),
    )


def issue_state_webhook_evidence(
    account_name: str,
    *,
    issue_id: str,
    state_id: str,
    revision: str,
    action: str = "update",
) -> WebhookEffectEvidence:
    """Build exact evidence for one Linear issue-state callback."""
    return WebhookEffectEvidence(
        scope=WebhookEffectScope(
            provider="linear",
            account=account_name,
            event_type="Issue",
            event_action=action,
            subject_id=issue_id,
        ),
        fingerprint=_fingerprint(
            "linear-issue-state",
            action=action,
            issue_id=issue_id,
            revision=revision,
            state_id=state_id,
        ),
    )


def normalize_comment_create_response(
    comment: dict[str, Any],
    issue_id: str,
) -> dict[str, Any]:
    """Normalize only response fields that prove the echo is the write we made."""
    comment_id = comment.get("id")
    created_at = comment.get("createdAt")
    updated_at = comment.get("updatedAt")
    issue = comment.get("issue")
    response_issue_id = (
        comment.get("issueId")
        if isinstance(comment.get("issueId"), str)
        else issue.get("id")
        if isinstance(issue, dict)
        else None
    )
    evidence = (comment_id, response_issue_id, created_at, updated_at)
    if not all(isinstance(value, str) and value for value in evidence):
        raise LinearError(_COMMENT_EVIDENCE_MISSING)
    if response_issue_id != issue_id:
        raise LinearError(_COMMENT_ISSUE_MISMATCH)
    return {
        **comment,
        "id": comment_id,
        "issueId": response_issue_id,
        "createdAt": created_at,
        "updatedAt": updated_at,
    }


def normalize_issue_state_update_response(
    issue: dict[str, Any],
    *,
    issue_id: str,
    state_id: str,
) -> dict[str, Any]:
    """Normalize only response fields shared with a Linear Issue/update callback."""
    response_issue_id = issue.get("id")
    updated_at = issue.get("updatedAt")
    state = issue.get("state")
    response_state_id = state.get("id") if isinstance(state, dict) else None
    evidence = (response_issue_id, response_state_id, updated_at)
    if not all(isinstance(value, str) and value for value in evidence):
        raise LinearError(_ISSUE_STATE_EVIDENCE_MISSING)
    if response_issue_id != issue_id:
        raise LinearError(_ISSUE_STATE_ISSUE_MISMATCH)
    if response_state_id != state_id:
        raise LinearError(_ISSUE_STATE_TARGET_MISMATCH)
    return {
        "id": response_issue_id,
        "stateId": response_state_id,
        "updatedAt": updated_at,
        "stateType": state.get("type") if isinstance(state, dict) else None,
    }
