"""Capability-driven delivery for messages that update across multiple flushes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, cast, runtime_checkable

from pynchy.host.orchestrator.messaging.sender import outbound_delivery_lock, resolve_target_jid
from pynchy.identifiers import (
    ChannelName,
    ChatJid,
)
from pynchy.logger import logger
from pynchy.plugins.api import (  # noqa: TC001 - beartype resolves contract annotations at runtime.
    Channel,
    OutboundEvent,
)
from pynchy.state import api as state
from pynchy.state.api import OutboundDelivery, OutboundDeliveryOperation


@runtime_checkable
class UpdatingDeps(Protocol):
    """Dependencies required for capability-driven channel updates."""

    @property
    def channels(self) -> list[Channel]: ...


@dataclass(frozen=True)
class UpdatingMessage:
    """Remote message and content available for the next in-place update."""

    message_id: str
    content: str


@dataclass(frozen=True)
class _DeliveryPlan:
    channel: Channel
    target_jid: str
    event: OutboundEvent
    operation: OutboundDeliveryOperation
    previous: UpdatingMessage | None


@dataclass(frozen=True)
class _PostResult:
    delivered: bool
    message: UpdatingMessage | None


async def deliver_updating_event(
    deps: UpdatingDeps,
    chat_jid: str,
    delta_event: OutboundEvent,
    messages: dict[str, UpdatingMessage],
    *,
    source: str,
) -> dict[str, UpdatingMessage]:
    """Append one event to editable channel messages, posting when necessary.

    Update-capable channels keep one remote message per consecutive run.
    Send-only channels receive only the current delta. If an edit fails, the
    accumulated content is posted separately and that message becomes the edit
    anchor when the channel returns an ID.
    """
    async with outbound_delivery_lock(chat_jid):
        plans = _delivery_plans(deps, chat_jid, delta_event, messages)
        ledger_ids = await _record_deliveries(chat_jid, source, plans)
        next_messages = dict(messages)
        for plan, ledger_id in zip(plans, ledger_ids, strict=True):
            result = await _deliver_plan(plan, ledger_id)
            if result is None:
                next_messages.pop(plan.channel.name, None)
            else:
                next_messages[plan.channel.name] = result
    return next_messages


def _delivery_plans(
    deps: UpdatingDeps,
    chat_jid: str,
    delta_event: OutboundEvent,
    messages: dict[str, UpdatingMessage],
) -> list[_DeliveryPlan]:
    plans: list[_DeliveryPlan] = []
    for channel in deps.channels:
        if not channel.is_connected():
            continue
        target_jid = resolve_target_jid(chat_jid, channel)
        if target_jid is None:
            continue
        previous = messages.get(channel.name)
        can_update = callable(getattr(channel, "post_event", None)) and callable(
            getattr(channel, "update_event", None)
        )
        if previous is not None and can_update:
            event = replace(
                delta_event,
                content=f"{previous.content}\n{delta_event.content}",
            )
            operation = OutboundDeliveryOperation.EDIT
        else:
            event = delta_event
            operation = OutboundDeliveryOperation.POST
            previous = None
        plans.append(
            _DeliveryPlan(
                channel=channel,
                target_jid=target_jid,
                event=event,
                operation=operation,
                previous=previous,
            )
        )
    return plans


async def _record_deliveries(
    chat_jid: str,
    source: str,
    plans: list[_DeliveryPlan],
) -> list[int | None]:
    """Normalize channels with identical content onto one ledger row."""
    groups: dict[str, list[tuple[int, _DeliveryPlan]]] = {}
    for index, plan in enumerate(plans):
        groups.setdefault(plan.event.content, []).append((index, plan))

    ledger_ids: list[int | None] = [None] * len(plans)
    for content, indexed_plans in groups.items():
        try:
            ledger_id = await state.record_outbound_deliveries(
                ChatJid(chat_jid),
                content,
                source,
                [
                    OutboundDelivery(
                        channel_name=ChannelName(plan.channel.name),
                        operation=plan.operation,
                        remote_message_id=(
                            plan.previous.message_id if plan.previous is not None else None
                        ),
                    )
                    for _, plan in indexed_plans
                ],
            )
        except Exception:  # noqa: BLE001 - ledger failure must not block channel delivery.
            logger.debug("Outbound update ledger write failed (fire-and-forget fallback)")
            continue
        for index, _ in indexed_plans:
            ledger_ids[index] = ledger_id
    return ledger_ids


async def _deliver_plan(
    plan: _DeliveryPlan,
    ledger_id: int | None,
) -> UpdatingMessage | None:
    if plan.operation is OutboundDeliveryOperation.EDIT:
        return await _update_or_fallback(plan, ledger_id)
    result = await _post_or_send(plan, ledger_id, OutboundDeliveryOperation.POST)
    return result.message


async def _update_or_fallback(
    plan: _DeliveryPlan,
    ledger_id: int | None,
) -> UpdatingMessage | None:
    # _delivery_plans creates EDIT plans only with both invariants satisfied.
    previous = cast("UpdatingMessage", plan.previous)
    update_event = plan.channel.update_event
    try:
        await update_event(plan.target_jid, previous.message_id, plan.event)
    except Exception as exc:  # noqa: BLE001 - a failed edit must fall back to a visible post.
        logger.warning(
            "Channel update failed, falling back to a new message",
            channel=plan.channel.name,
            err=str(exc),
        )
        fallback = await _post_or_send(
            plan,
            ledger_id,
            OutboundDeliveryOperation.FALLBACK_POST,
        )
        return fallback.message if fallback.delivered else previous

    await _mark_delivery_result(
        ledger_id,
        plan.channel.name,
        OutboundDeliveryOperation.EDIT,
        previous.message_id,
    )
    return UpdatingMessage(message_id=previous.message_id, content=plan.event.content)


async def _post_or_send(
    plan: _DeliveryPlan,
    ledger_id: int | None,
    operation: OutboundDeliveryOperation,
) -> _PostResult:
    post_event = getattr(plan.channel, "post_event", None)
    if callable(post_event):
        try:
            message_id = await post_event(plan.target_jid, plan.event)
        except Exception as exc:  # noqa: BLE001 - failed editable posts use send_event.
            logger.warning(
                "Channel post failed, falling back to send_event",
                channel=plan.channel.name,
                err=str(exc),
            )
        else:
            if message_id:
                await _mark_delivery_result(
                    ledger_id,
                    plan.channel.name,
                    operation,
                    message_id,
                )
                return _PostResult(
                    delivered=True,
                    message=UpdatingMessage(
                        message_id=message_id,
                        content=plan.event.content,
                    ),
                )

    try:
        await plan.channel.send_event(plan.target_jid, plan.event)
    except Exception as exc:  # noqa: BLE001 - failed delivery remains retryable in the ledger.
        logger.warning("Channel send failed", channel=plan.channel.name, err=str(exc))
        await _mark_error(ledger_id, plan.channel.name, str(exc))
        return _PostResult(delivered=False, message=None)

    await _mark_delivery_result(ledger_id, plan.channel.name, operation, None)
    return _PostResult(delivered=True, message=None)


async def _mark_delivery_result(
    ledger_id: int | None,
    channel_name: str,
    operation: OutboundDeliveryOperation,
    remote_message_id: str | None,
) -> None:
    if ledger_id is None:
        return
    try:
        await state.mark_delivery_succeeded(
            ledger_id,
            channel_name,
            operation,
            remote_message_id,
        )
    except Exception:  # noqa: BLE001 - ledger marking is best-effort bookkeeping.
        logger.debug("Ledger delivery result failed (best-effort)", channel=channel_name)


async def _mark_error(ledger_id: int | None, channel_name: str, error: str) -> None:
    if ledger_id is None:
        return
    try:
        await state.mark_delivery_error(ledger_id, channel_name, error)
    except Exception:  # noqa: BLE001 - ledger error marking is best-effort bookkeeping.
        logger.debug("Ledger mark_delivery_error failed (best-effort)", channel=channel_name)
