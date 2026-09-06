"""Channel sends and edits with shared delivery and retry bookkeeping.

Record intended mutations before provider I/O under a per-chat lock, so the
reconciler cannot duplicate a send in progress. Ledger failures remain best-effort:
they must not turn a successful provider write into a retry or block delivery.
"""

from __future__ import annotations

from asyncio import Lock
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable
from weakref import WeakValueDictionary

from pynchy.identifiers import ChannelName, ChatJid
from pynchy.logger import logger
from pynchy.plugins.api import (
    Channel,
    OutboundEvent,
)
from pynchy.state import api as state
from pynchy.state.api import OutboundDelivery, OutboundDeliveryOperation


@runtime_checkable
class BusDeps(Protocol):
    """Channels available to the outbound message bus."""

    @property
    def channels(self) -> list[Channel]: ...


@dataclass(frozen=True)
class UpdatingMessage:
    """Remote message and content available for the next in-place update."""

    message_id: str
    content: str


@dataclass
class _Delivery:
    channel: Channel
    event: OutboundEvent
    message_id: str | None = None
    ledger_id: int | None = None

    @property
    def operation(self) -> OutboundDeliveryOperation:
        return (
            OutboundDeliveryOperation.EDIT
            if self.message_id is not None
            else OutboundDeliveryOperation.POST
        )


@dataclass(frozen=True)
class _DeliveryResult:
    delivered: bool
    message_id: str | None = None


_outbound_delivery_locks: WeakValueDictionary[str, Lock] = WeakValueDictionary()


def outbound_delivery_lock(chat_jid: str) -> Lock:
    """Serialize ledger-backed provider sends for one chat in this host process."""
    lock = _outbound_delivery_locks.get(chat_jid)
    if lock is None:
        lock = Lock()
        _outbound_delivery_locks[chat_jid] = lock
    return lock


def resolve_target_jid(chat_jid: str, channel: Channel) -> str | None:
    """Return the canonical JID only when the channel owns it."""
    return chat_jid if channel.owns_jid(chat_jid) else None


async def broadcast(  # noqa: PLR0913 - outbound delivery keeps routing and error policy explicit.
    deps: BusDeps,
    chat_jid: str,
    event: OutboundEvent,
    *,
    suppress_errors: bool = True,
    skip_channel: str | None = None,
    source: str = "broadcast",
    stream_message_ids: dict[str, str] | None = None,
) -> bool:
    """Send an event, replacing existing stream messages when IDs are supplied.

    Channels without a usable stream ID receive a normal send. A failed stream
    edit falls back to a send. Disconnected channels retain a pending ledger row
    for reconciliation. Return whether any channel accepted the event.

    ``suppress_errors=True`` catches network errors; False catches all Exceptions.
    This caller policy also applies to sends after a failed edit.
    """
    plans: list[_Delivery] = []
    for channel in deps.channels:
        if skip_channel and channel.name == skip_channel:
            continue
        if not resolve_target_jid(chat_jid, channel):
            continue
        message_id = (stream_message_ids or {}).get(channel.name)
        if not message_id or not hasattr(channel, "update_event"):
            message_id = None
        plans.append(_Delivery(channel, event, message_id))

    # Finalize existing streams before posting to other channels. An existing
    # stream ID remains worth attempting even if connection status is stale.
    plans.sort(key=lambda plan: plan.message_id is None)
    targets = [plan for plan in plans if plan.message_id is not None or plan.channel.is_connected()]
    caught = (OSError, TimeoutError, ConnectionError) if suppress_errors else (Exception,)
    async with outbound_delivery_lock(chat_jid):
        await _record_deliveries(chat_jid, source, plans)
        delivered = False
        for plan in targets:
            result = await _deliver(plan, chat_jid, caught=caught)
            delivered = result.delivered or delivered
    return delivered


async def deliver_updating_event(
    deps: BusDeps,
    chat_jid: str,
    delta_event: OutboundEvent,
    messages: dict[str, UpdatingMessage],
    *,
    source: str,
) -> dict[str, UpdatingMessage]:
    """Append a delta to editable messages and return their next edit anchors.

    Send-only channels receive the delta alone. An edit failure posts accumulated
    content, which becomes its edit anchor when possible. If every delivery
    attempt fails, retain the existing anchor and content for the next flush.
    """
    async with outbound_delivery_lock(chat_jid):
        plans = _updating_deliveries(deps, chat_jid, delta_event, messages)
        await _record_deliveries(chat_jid, source, plans)
        next_messages = dict(messages)
        for plan in plans:
            result = await _deliver(plan, chat_jid, keep_editing=True)
            if result.delivered and result.message_id is not None:
                next_messages[plan.channel.name] = UpdatingMessage(
                    result.message_id, plan.event.content
                )
            elif result.delivered or plan.message_id is None:
                next_messages.pop(plan.channel.name, None)
    return next_messages


def _updating_deliveries(
    deps: BusDeps,
    chat_jid: str,
    delta_event: OutboundEvent,
    messages: dict[str, UpdatingMessage],
) -> list[_Delivery]:
    plans: list[_Delivery] = []
    for channel in deps.channels:
        if not channel.is_connected() or resolve_target_jid(chat_jid, channel) is None:
            continue
        previous = messages.get(channel.name)
        can_update = callable(getattr(channel, "post_event", None)) and callable(
            getattr(channel, "update_event", None)
        )
        if previous is not None and can_update:
            plans.append(
                _Delivery(
                    channel,
                    replace(delta_event, content=f"{previous.content}\n{delta_event.content}"),
                    previous.message_id,
                )
            )
        else:
            plans.append(_Delivery(channel, delta_event))
    return plans


async def _record_deliveries(
    chat_jid: str,
    source: str,
    plans: list[_Delivery],
) -> None:
    """Store identical content once, with its per-channel mutation intentions."""
    groups: dict[str, list[_Delivery]] = {}
    for plan in plans:
        groups.setdefault(plan.event.content, []).append(plan)
    for content, grouped_plans in groups.items():
        try:
            ledger_id = await state.record_outbound_deliveries(
                ChatJid(chat_jid),
                content,
                source,
                [
                    OutboundDelivery(
                        ChannelName(plan.channel.name), plan.operation, plan.message_id
                    )
                    for plan in grouped_plans
                ],
            )
        except Exception:  # noqa: BLE001 - ledger failure must not block channel delivery.
            logger.debug("Outbound ledger write failed (fire-and-forget fallback)")
        else:
            for plan in grouped_plans:
                plan.ledger_id = ledger_id


async def _deliver(
    plan: _Delivery,
    chat_jid: str,
    *,
    keep_editing: bool = False,
    caught: tuple[type[BaseException], ...] = (Exception,),
) -> _DeliveryResult:
    channel = plan.channel
    ledger_id = plan.ledger_id
    operation = plan.operation
    if plan.message_id is not None:
        try:
            await channel.update_event(chat_jid, plan.message_id, plan.event)
        except Exception as exc:  # noqa: BLE001 - failed edits fall back to a visible post.
            logger.warning(
                "Channel update failed, falling back to a new message",
                channel=channel.name,
                err=str(exc),
            )
            operation = OutboundDeliveryOperation.FALLBACK_POST
        else:
            await _mark_success(ledger_id, channel.name, operation, plan.message_id)
            return _DeliveryResult(delivered=True, message_id=plan.message_id)

    post_event = getattr(channel, "post_event", None) if keep_editing else None
    if callable(post_event):
        try:
            message_id = await post_event(chat_jid, plan.event)
        except Exception as exc:  # noqa: BLE001 - failed editable posts use send_event.
            logger.warning(
                "Channel post failed, falling back to send_event",
                channel=channel.name,
                err=str(exc),
            )
        else:
            if message_id:
                await _mark_success(ledger_id, channel.name, operation, message_id)
                return _DeliveryResult(delivered=True, message_id=message_id)

    try:
        await channel.send_event(chat_jid, plan.event)
    except caught as exc:
        logger.warning("Channel send failed", channel=channel.name, err=str(exc))
        await _mark_error(ledger_id, channel.name, str(exc))
        return _DeliveryResult(delivered=False)
    await _mark_success(ledger_id, channel.name, operation, None)
    return _DeliveryResult(delivered=True)


async def _mark_success(
    ledger_id: int | None,
    channel_name: str,
    operation: OutboundDeliveryOperation,
    remote_message_id: str | None,
) -> None:
    if ledger_id is None:
        return
    try:
        await state.mark_delivery_succeeded(ledger_id, channel_name, operation, remote_message_id)
    except Exception:  # noqa: BLE001 - ledger marking must not retry a successful provider write.
        logger.debug("Ledger delivery result failed (best-effort)", channel=channel_name)


async def _mark_error(ledger_id: int | None, channel_name: str, error: str) -> None:
    if ledger_id is None:
        return
    try:
        await state.mark_delivery_error(ledger_id, channel_name, error)
    except Exception:  # noqa: BLE001 - ledger error marking is best-effort bookkeeping.
        logger.debug("Ledger mark_delivery_error failed (best-effort)", channel=channel_name)
