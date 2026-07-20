"""Host-side coordination for descriptor-declared transactional external writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pynchy.action_intents import ActionIntent, ActionIntentStatus
from pynchy.capabilities import (  # noqa: TC001, RUF100 - beartype resolves runtime annotations.
    HostActionDescriptor,
)
from pynchy.logger import logger
from pynchy.state import (
    ActionIntentCreateRequest,
    claim_action_intent,
    confirm_action_intent,
    create_action_intent,
    fail_action_intent,
    get_action_intent_by_request,
    mark_action_intent_executing,
    mark_action_intent_outcome_unknown,
)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


async def prepare_action_intent(
    action: HostActionDescriptor,
    data: dict[str, Any],
    *,
    workspace: str,
    chat_jid: str,
    request_id: str,
) -> tuple[ActionIntent | None, dict[str, Any] | None]:
    """Create an immutable draft or return the safe outcome for a replay."""
    contract = action.action_intent
    if contract is None:
        return None, None

    existing = await get_action_intent_by_request(request_id)
    if existing is not None and existing.status not in {
        ActionIntentStatus.DRAFTED,
        ActionIntentStatus.APPROVED,
    }:
        return existing, action_intent_replay_response(existing)

    try:
        draft = contract.draft_from_request(data)
    except (TypeError, ValueError) as exc:
        return None, {"error": f"Invalid external action arguments: {exc}"}

    intent, created = await create_action_intent(
        ActionIntentCreateRequest(
            request_id=request_id,
            workspace=workspace,
            action_id=str(action.capability.action_ids[0]),
            tool_name=str(action.tool_name),
            provider=contract.provider,
            actor_jid=chat_jid,
            recipient=draft.recipient,
            payload=draft.payload,
            source_refs=(f"ipc:{request_id}", f"chat:{chat_jid}"),
            summary=draft.summary,
        )
    )
    replay_response = None
    if not created and intent.status not in {
        ActionIntentStatus.DRAFTED,
        ActionIntentStatus.APPROVED,
    }:
        replay_response = action_intent_replay_response(intent)
    return intent, replay_response


def action_intent_replay_response(intent: ActionIntent) -> dict[str, Any]:
    """Return a terminal result without ever repeating a provider side effect."""
    if intent.status is ActionIntentStatus.CONFIRMED and intent.provider_receipt is not None:
        return {"result": json.dumps(intent.provider_receipt, sort_keys=True)}
    if intent.status is ActionIntentStatus.OUTCOME_UNKNOWN:
        return {
            "error": "External action outcome is unknown; reconcile the provider before retrying."
        }
    if intent.status is ActionIntentStatus.AWAITING_APPROVAL:
        return {"error": "External action is awaiting human approval."}
    if intent.status in {
        ActionIntentStatus.DENIED,
        ActionIntentStatus.EXPIRED,
        ActionIntentStatus.FAILED,
    }:
        return {"error": intent.error or f"External action {intent.status.value}."}
    return {"error": f"External action is already {intent.status.value}; it was not re-executed."}


async def execute_action_intent(
    action: HostActionDescriptor,
    data: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    """Claim, invoke once, and persist a provider receipt or unknown outcome."""
    contract = action.action_intent
    if contract is not None:
        validation_error = await _validate_current_intent(action, data, request_id)
        if validation_error is not None:
            return validation_error
        claimed = await claim_action_intent(request_id)
        if claimed is None:
            return {"error": "External action record is missing; refusing to send."}
        if claimed.status is not ActionIntentStatus.CLAIMED:
            return action_intent_replay_response(claimed)
        await mark_action_intent_executing(request_id)
        return await _record_action_intent_attempt(action, data, request_id=request_id)
    return await action.handler(data)


async def _validate_current_intent(
    action: HostActionDescriptor,
    data: dict[str, Any],
    request_id: str,
) -> dict[str, Any] | None:
    """Prove the destination and payload still match the durable approval draft."""
    contract = action.action_intent
    existing = await load_action_intent(request_id)
    if contract is None or existing is None:
        return {"error": "External action record is missing; refusing to send."}
    try:
        current = contract.draft_from_request(data)
    except (TypeError, ValueError) as exc:
        await fail_action_intent(
            request_id,
            reason=f"Approved payload is no longer valid: {exc}",
        )
        return {"error": "Approved external action is no longer valid; request it again."}
    if current.recipient == existing.recipient and current.payload == existing.payload:
        return None
    await fail_action_intent(
        request_id,
        reason="Approved destination or payload changed before execution.",
    )
    return {"error": "Approved external action changed; request a new approval."}


async def _record_action_intent_attempt(
    action: HostActionDescriptor,
    data: dict[str, Any],
    *,
    request_id: str,
) -> dict[str, Any]:
    """Persist the result of one provider attempt after its durable claim."""
    contract = action.action_intent
    if contract is None:
        raise RuntimeError("Transactional action lost its intent contract")
    try:
        response = await action.handler(data)
    except Exception as exc:  # noqa: BLE001, RUF100 - provider call outcome cannot be proven after an exception.
        logger.warning(
            "External action provider call produced no durable receipt",
            request_id=request_id,
            action_id=str(action.capability.id),
            provider=contract.provider,
            error_type=type(exc).__name__,
        )
        await mark_action_intent_outcome_unknown(request_id, reason=type(exc).__name__)
        return {"error": "External action outcome is unknown; do not retry automatically."}

    if "error" in response:
        await mark_action_intent_outcome_unknown(
            request_id,
            reason="Provider handler returned an error without a durable receipt.",
        )
        return {"error": "External action outcome is unknown; do not retry automatically."}

    try:
        receipt = contract.receipt_from_response(response)
    except (TypeError, ValueError, json.JSONDecodeError):
        await mark_action_intent_outcome_unknown(
            request_id,
            reason="Provider response lacked a valid durable receipt.",
        )
        return {"error": "External action outcome is unknown; do not retry automatically."}

    await confirm_action_intent(
        request_id,
        provider_request_id=receipt.provider_request_id,
        receipt=receipt.receipt,
    )
    return response


async def load_action_intent(request_id: str) -> ActionIntent | None:
    """Return a previously persisted intent for approval replay."""
    return await get_action_intent_by_request(request_id)


def policy_approval_timestamp() -> str:
    """Supply a durable timestamp when policy permits an action without a prompt."""
    return _timestamp()
