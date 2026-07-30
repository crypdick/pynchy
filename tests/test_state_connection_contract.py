"""Contract tests for state initialization."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from pynchy.conversation.models import (
    ControlSurface,
    ConversationControlBinding,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalDeliveryReceipt,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import (
    ChatJid,
    GroupFolder,
    SessionId,
)
from pynchy.state import (
    admit_conversation_delivery,
    admit_external_delivery_receipt,
    close_test_database,
    get_all_chats,
    get_conversation,
    get_conversation_control_binding,
    get_conversation_for_subject_key,
    get_session,
    init_database,
    record_outbound,
    resolve_conversation,
    set_conversation_control_binding,
    set_conversation_session,
    set_session,
    store_message_direct,
)
from pynchy.state.connection import StateRuntimeConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_state_database_access_requires_initialization() -> None:
    close_test_database()
    close_test_database()

    with pytest.raises(RuntimeError, match="Database not initialized"):
        await get_all_chats()


@pytest.mark.asyncio
async def test_init_database_uses_explicit_runtime_config(tmp_path: Path) -> None:
    database_path = tmp_path / "explicit" / "messages.db"
    close_test_database()

    try:
        await init_database(StateRuntimeConfig(database_path=database_path))

        assert await get_all_chats() == []
        assert database_path.is_file()
    finally:
        close_test_database()


@pytest.mark.asyncio
async def test_initialization_repairs_chat_parents_and_writers_preserve_integrity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as legacy:
        legacy.executescript(
            """
            CREATE TABLE chats (
                jid TEXT PRIMARY KEY,
                name TEXT,
                last_message_time TEXT,
                cleared_at TEXT
            );
            CREATE TABLE messages (
                id TEXT,
                chat_jid TEXT,
                sender TEXT,
                sender_name TEXT,
                content TEXT,
                timestamp TEXT,
                is_from_me INTEGER,
                message_type TEXT DEFAULT 'user',
                metadata TEXT,
                PRIMARY KEY (id, chat_jid),
                FOREIGN KEY (chat_jid) REFERENCES chats(jid)
            );
            CREATE TABLE outbound_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_jid TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                FOREIGN KEY (chat_jid) REFERENCES chats(jid)
            );
            INSERT INTO messages (
                id, chat_jid, sender, sender_name, content, timestamp, is_from_me
            ) VALUES (
                'legacy-message', 'discord:legacy', 'human', 'Human', 'hello',
                '2026-07-26T00:00:00+00:00', 0
            );
            INSERT INTO outbound_ledger (chat_jid, content, timestamp, source)
            VALUES (
                'discord:legacy', 'reply', '2026-07-26T00:01:00+00:00', 'test'
            );
            """
        )
    close_test_database()

    try:
        await init_database(StateRuntimeConfig(database_path=database_path))
        await store_message_direct(
            message_id="new-message",
            chat_jid="discord:new-message",
            sender="human",
            sender_name="Human",
            content="hello",
            timestamp="2026-07-26T00:02:00+00:00",
            is_from_me=False,
        )
        await record_outbound(
            ChatJid("discord:new-outbound"),
            "reply",
            "test",
            ["discord"],
        )

        assert {chat["jid"] for chat in await get_all_chats()} == {
            "discord:legacy",
            "discord:new-message",
            "discord:new-outbound",
        }
    finally:
        close_test_database()

    with sqlite3.connect(database_path) as verified:
        assert verified.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
async def test_initialization_retires_older_linear_conversation_alias(tmp_path: Path) -> None:
    database_path = tmp_path / "duplicates.db"
    close_test_database()
    await init_database(StateRuntimeConfig(database_path=database_path))
    old = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:tenant-id:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("pynchy-dev"),
    )
    old_session = SessionId("codex:model:old")
    old_folder = GroupFolder(f"pynchy-dev__thread_conversation-{old.id}")
    await set_conversation_session(old.id, old_session)
    await set_session(old_folder, old_session)
    await set_conversation_control_binding(
        ConversationControlBinding(
            conversation_id=old.id,
            surface=ControlSurface.DISCORD,
            parent_workspace=GroupFolder("admin"),
            parent_jid=ChatJid("discord:channel:parent"),
            thread_jid=ChatJid("discord:channel:old"),
            title="[SYN-1] Old",
            updated_at="2026-07-26T00:00:00+00:00",
        )
    )
    current = await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:linear:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("pynchy-dev"),
    )
    close_test_database()

    try:
        await init_database(StateRuntimeConfig(database_path=database_path))

        retired = await get_conversation(old.id)
        binding = await get_conversation_control_binding(old.id)
        resolved = await get_conversation_for_subject_key(
            ConversationSubjectKey("issue-1"),
            workspace=GroupFolder("pynchy-dev"),
            namespace_suffix=":issue",
        )
        assert retired is not None
        assert retired.subject.namespace == ConversationSubjectNamespace(
            f"retired:linear-conversation:{old.id}"
        )
        assert retired.session_id is None
        assert binding is not None
        assert binding.closed is True
        assert await get_session(old_folder) is None
        assert resolved is not None
        assert resolved.id == current.id
    finally:
        close_test_database()


@pytest.mark.asyncio
async def test_initialization_rolls_back_repairs_when_duplicate_alias_owns_work(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "active-duplicate.db"
    close_test_database()
    await init_database(StateRuntimeConfig(database_path=database_path))
    old_subject = ConversationSubject(
        namespace=ConversationSubjectNamespace("linear:tenant-id:issue"),
        key=ConversationSubjectKey("issue-1"),
    )
    old = await resolve_conversation(old_subject, GroupFolder("pynchy-dev"))
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("route"),
        delivery_id=ExternalDeliveryId("pending-old"),
    )
    await admit_external_delivery_receipt(
        ExternalDeliveryReceipt(
            identity=identity,
            payload_sha256="sha256",
            received_at="2020-01-01T00:00:00+00:00",
        )
    )
    await admit_conversation_delivery(identity, old_subject, GroupFolder("pynchy-dev"))
    await resolve_conversation(
        ConversationSubject(
            namespace=ConversationSubjectNamespace("linear:linear:issue"),
            key=ConversationSubjectKey("issue-1"),
        ),
        GroupFolder("pynchy-dev"),
    )
    close_test_database()
    with sqlite3.connect(database_path) as legacy:
        legacy.execute(
            """
            INSERT INTO messages (
                id, chat_jid, sender, sender_name, content, timestamp, is_from_me
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "orphan",
                "discord:rollback",
                "human",
                "Human",
                "hello",
                "2026-07-26T00:00:00+00:00",
                0,
            ),
        )

    try:
        with pytest.raises(RuntimeError, match="still owns active runtime state"):
            await init_database(StateRuntimeConfig(database_path=database_path))
    finally:
        close_test_database()

    with sqlite3.connect(database_path) as verified:
        assert (
            verified.execute("SELECT 1 FROM chats WHERE jid = 'discord:rollback'").fetchone()
            is None
        )
        assert verified.execute(
            "SELECT subject_namespace FROM routed_conversations WHERE id = ?",
            (old.id,),
        ).fetchone() == ("linear:tenant-id:issue",)
