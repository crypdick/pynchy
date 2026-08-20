"""Durable serialization for deferred trusted webhook processing."""

from __future__ import annotations

from collections.abc import Mapping

from pynchy.conversation.api import (
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.identifiers import ChatJid
from pynchy.plugins.api import (
    WebhookActor,
    WebhookConversation,
    WebhookEvent,
)
from pynchy.webhook_effects import (
    WebhookEffectEvidence,
    WebhookEffectScope,
)


def webhook_event_payload(event: WebhookEvent) -> dict[str, object]:
    """Serialize every route-processing input needed after a durable hold."""
    conversation = event.conversation
    actor = event.actor
    evidence = event.effect_evidence
    return {
        "delivery_id": event.delivery_id,
        "event_type": event.event_type,
        "action": event.action,
        "subject_id": event.subject_id,
        "occurred_at": event.occurred_at,
        "instructions": event.instructions,
        "external_context": (
            dict(event.external_context)
            if isinstance(event.external_context, Mapping)
            else event.external_context
        ),
        "ignored_reason": event.ignored_reason,
        "host_message": event.host_message,
        "conversation": (
            {
                "subject_namespace": conversation.subject.namespace,
                "subject_key": conversation.subject.key,
                "control_title": conversation.control_title,
                "control_closed": conversation.control_closed,
                "control_state_revision": conversation.control_state_revision,
                "workspace": conversation.workspace,
                "controller_workspace": conversation.controller_workspace,
                "public_source": conversation.public_source,
                "notification_jid": conversation.notification_jid,
            }
            if conversation is not None
            else None
        ),
        "actor": ({"id": actor.id, "kind": actor.kind} if actor is not None else None),
        "changed_fields": sorted(event.changed_fields),
        "effect_evidence": (
            {
                "provider": evidence.scope.provider,
                "account": evidence.scope.account,
                "event_type": evidence.scope.event_type,
                "event_action": evidence.scope.event_action,
                "subject_id": evidence.scope.subject_id,
                "intent_fingerprint": evidence.scope.intent_fingerprint,
                "fingerprint": evidence.fingerprint,
            }
            if evidence is not None
            else None
        ),
    }


def _optional_mapping(value: object, field: str) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError(f"Deferred webhook {field} is not an object")
    return value


def _required_text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Deferred webhook {field} is not a non-empty string")
    return value


def _optional_text(payload: Mapping[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is not None and not isinstance(value, str):
        raise TypeError(f"Deferred webhook {field} is not a string")
    return value


def _conversation(payload: Mapping[str, object]) -> WebhookConversation | None:
    raw = _optional_mapping(payload.get("conversation"), "conversation")
    if raw is None:
        return None
    closed = raw.get("control_closed")
    control_state_revision = raw.get("control_state_revision")
    public_source = raw.get("public_source")
    notification_jid = _optional_text(raw, "notification_jid")
    if closed is not None and not isinstance(closed, bool):
        raise TypeError("Deferred webhook control_closed is not boolean")
    if control_state_revision is not None and not isinstance(control_state_revision, str):
        raise TypeError("Deferred webhook control_state_revision is not a string")
    if public_source is not None and not isinstance(public_source, bool):
        raise TypeError("Deferred webhook public_source is not boolean")
    return WebhookConversation(
        subject=ConversationSubject(
            namespace=ConversationSubjectNamespace(_required_text(raw, "subject_namespace")),
            key=ConversationSubjectKey(_required_text(raw, "subject_key")),
        ),
        control_title=_required_text(raw, "control_title"),
        control_closed=closed,
        control_state_revision=control_state_revision,
        workspace=_optional_text(raw, "workspace"),
        controller_workspace=_optional_text(raw, "controller_workspace"),
        public_source=public_source,
        notification_jid=ChatJid(notification_jid) if notification_jid is not None else None,
    )


def _actor(payload: Mapping[str, object]) -> WebhookActor | None:
    raw = _optional_mapping(payload.get("actor"), "actor")
    if raw is None:
        return None
    return WebhookActor(
        id=_required_text(raw, "id"),
        kind=_required_text(raw, "kind"),
    )


def _effect_evidence(payload: Mapping[str, object]) -> WebhookEffectEvidence | None:
    raw = _optional_mapping(payload.get("effect_evidence"), "effect evidence")
    if raw is None:
        return None
    return WebhookEffectEvidence(
        scope=WebhookEffectScope(
            provider=_required_text(raw, "provider"),
            account=_required_text(raw, "account"),
            event_type=_required_text(raw, "event_type"),
            event_action=_required_text(raw, "event_action"),
            subject_id=_required_text(raw, "subject_id"),
            intent_fingerprint=_optional_text(raw, "intent_fingerprint"),
        ),
        fingerprint=_required_text(raw, "fingerprint"),
    )


def webhook_event_from_payload(payload: Mapping[str, object]) -> WebhookEvent:
    """Reconstruct one authenticated event without mutable provider reads."""
    changed_fields = payload.get("changed_fields")
    if not isinstance(changed_fields, list) or not all(
        isinstance(field, str) for field in changed_fields
    ):
        raise TypeError("Deferred webhook changed_fields is not a string list")
    external_context = payload.get("external_context")
    if external_context is not None and not isinstance(external_context, str | Mapping):
        raise TypeError("Deferred webhook external_context has an invalid shape")
    return WebhookEvent(
        delivery_id=_required_text(payload, "delivery_id"),
        event_type=_required_text(payload, "event_type"),
        action=_required_text(payload, "action"),
        subject_id=_required_text(payload, "subject_id"),
        occurred_at=_required_text(payload, "occurred_at"),
        instructions=_optional_text(payload, "instructions"),
        external_context=external_context,
        ignored_reason=_optional_text(payload, "ignored_reason"),
        host_message=_optional_text(payload, "host_message"),
        conversation=_conversation(payload),
        actor=_actor(payload),
        changed_fields=frozenset(changed_fields),
        effect_evidence=_effect_evidence(payload),
    )
