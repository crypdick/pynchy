"""Public conversation-control state validation and lifecycle contracts."""

from __future__ import annotations

import pytest
from conftest import init_test_database

from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import ChatJid, GroupFolder
from pynchy.state import (
    apply_conversation_control_state,
    conversation_control_state_matches,
    get_conversation_control_binding,
    resolve_conversation,
    retire_conversation_for_terminal,
    set_conversation_control_binding,
)


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


def _delivery() -> ExternalDeliveryIdentity:
    return ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId("delivery-control-edge"),
    )


async def _conversation(key: str = "control-edge"):
    return await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:tenant:issue"),
            key=ConversationSubjectKey(key),
        ),
        GroupFolder("owner"),
    )


@pytest.mark.parametrize(
    "revision",
    ["not-a-timestamp", "2026-07-29T20:00:00"],
    ids=["invalid-format", "missing-timezone"],
)
async def test_control_state_rejects_invalid_provider_revision(revision: str) -> None:
    with pytest.raises(ValueError, match=r"ISO-8601 timestamp|timezone"):
        await apply_conversation_control_state(
            ConversationId("missing"),
            closed=True,
            control_state_revision=revision,
        )


async def test_control_state_rejects_unknown_conversation() -> None:
    with pytest.raises(ValueError, match="Unknown conversation"):
        await apply_conversation_control_state(
            ConversationId("missing"),
            closed=True,
            control_state_revision=None,
        )


async def test_control_match_requires_identity_and_claim_as_a_pair() -> None:
    with pytest.raises(ValueError, match="requires delivery identity and claim ID"):
        await conversation_control_state_matches(
            ConversationId("missing"),
            closed=False,
            control_state_revision=None,
            delivery_identity=_delivery(),
        )


async def test_control_match_returns_false_for_unknown_conversation() -> None:
    assert (
        await conversation_control_state_matches(
            ConversationId("missing"),
            closed=False,
            control_state_revision=None,
        )
        is False
    )


async def test_terminal_retirement_rejects_unknown_conversation() -> None:
    with pytest.raises(ValueError, match="Unknown conversation"):
        await retire_conversation_for_terminal(
            ConversationId("missing"),
            preserve_delivery=_delivery(),
        )


async def test_control_binding_rejects_unknown_conversation() -> None:
    binding = ConversationControlBinding(
        conversation_id=ConversationId("missing"),
        surface=ControlSurface.DISCORD,
        parent_workspace=GroupFolder("owner"),
        parent_jid=ChatJid("discord:channel:owner"),
        thread_jid=ChatJid("discord:channel:thread"),
        title="Control",
        updated_at="2026-07-29T20:00:00+00:00",
    )

    with pytest.raises(ValueError, match="Unknown conversation"):
        await set_conversation_control_binding(binding)


async def test_terminal_conversation_forces_new_binding_closed() -> None:
    conversation = await _conversation()
    await apply_conversation_control_state(
        conversation.id,
        closed=True,
        control_state_revision="2026-07-29T20:00:00+00:00",
    )
    binding = ConversationControlBinding(
        conversation_id=conversation.id,
        surface=ControlSurface.DISCORD,
        parent_workspace=GroupFolder("owner"),
        parent_jid=ChatJid("discord:channel:owner"),
        thread_jid=ChatJid("discord:channel:thread"),
        title="Control",
        updated_at="2026-07-29T20:00:01+00:00",
        closed=False,
    )

    stored = await set_conversation_control_binding(binding)

    assert stored.closed is True
    assert (await get_conversation_control_binding(conversation.id)).closed is True
