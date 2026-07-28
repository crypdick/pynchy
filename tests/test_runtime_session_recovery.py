"""Recovery coverage for provider sessions unavailable after host restart."""

from __future__ import annotations

import pytest
from conftest import init_test_database

from pynchy.agent_protocol.api import (
    InFlightTurn,
    InFlightWorkKind,
)
from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
)
from pynchy.conversation.workspaces import routed_conversation_folder
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.state import (
    begin_in_flight_turn,
    get_conversation,
    get_in_flight_turn,
    get_session,
    get_session_security_taint,
    mark_session_security_taint,
    resolve_conversation,
    set_conversation_control_binding,
    set_conversation_session,
    set_session,
)
from pynchy.state.runtime_session_recovery import clear_runtime_session_references


@pytest.fixture(autouse=True)
async def _database() -> None:
    await init_test_database()


async def test_unavailable_session_clears_ids_but_preserves_security_taint() -> None:
    thread_jid = ChatJid("discord:channel:unavailable-session")
    conversation = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear"),
            key=ConversationSubjectKey("issue-unavailable-session"),
        ),
        GroupFolder("pynchy-dev"),
    )
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=conversation.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("pynchy-dev"),
            parent_jid=ChatJid("discord:channel:pynchy-dev"),
            thread_jid=thread_jid,
            title="[SYN-35] Routed control",
            updated_at="2026-07-19T12:00:00+00:00",
        )
    )
    session_id = SessionId("codex:model:missing-thread")
    folder = GroupFolder(routed_conversation_folder("pynchy-dev", conversation.id))
    await set_conversation_session(conversation.id, session_id)
    await set_session(folder, session_id)
    await mark_session_security_taint(folder, secret_tainted=True)
    turn = InFlightTurn(
        turn_id="turn-unavailable-session",
        chat_jid=thread_jid,
        group_folder=folder,
        work_kind=InFlightWorkKind.INTERACTIVE,
        input_messages=[{"content": "resume"}],
        input_start_cursor="2026-07-19T11:59:00+00:00",
        input_end_cursor="2026-07-19T12:00:00+00:00",
        started_at="2026-07-19T12:00:01+00:00",
        session_id=session_id,
    )
    await begin_in_flight_turn(turn)

    await clear_runtime_session_references(folder, session_id, thread_jid)

    cleared_conversation = await get_conversation(conversation.id)
    cleared_turn = await get_in_flight_turn(turn.turn_id)
    assert cleared_conversation is not None
    assert cleared_conversation.session_id is None
    assert await get_session(folder) is None
    assert (await get_session_security_taint(folder)).secret_tainted is True
    assert cleared_turn is not None
    assert cleared_turn.session_id is None
