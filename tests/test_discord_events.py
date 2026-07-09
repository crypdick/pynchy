"""Discord inbound messages as emitted by the public channel adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pynchy.config.models import DiscordConnectionConfig
from pynchy.plugins.channels.discord import DiscordChannel

if TYPE_CHECKING:
    from pynchy.types import NewMessage

BOT_ID = "999"
DISCORD_BOT_ENV = "X"


def _user(
    uid: str,
    *,
    bot: bool = False,
    roles: tuple[str, ...] = (),
    display_name: str | None = None,
    global_name: str | None = None,
    name: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uid,
        bot=bot,
        roles=[SimpleNamespace(id=r) for r in roles],
        display_name=display_name,
        global_name=global_name,
        name=name,
    )


def _message(
    *,
    author: SimpleNamespace,
    guild_id: str | None,
    channel_id: str,
    guild_name: str | None = None,
    channel_name: str | None = None,
    parent_id: str | None = None,
    parent_name: str | None = None,
    mentions: tuple[str, ...] = (),
    content: str = "",
    attachments: tuple[SimpleNamespace, ...] = (),
    reference: SimpleNamespace | None = None,
    message_snapshots: tuple[SimpleNamespace, ...] = (),
) -> SimpleNamespace:
    guild = None if guild_id is None else SimpleNamespace(id=guild_id, name=guild_name)
    parent = None
    if parent_id is not None:
        parent = SimpleNamespace(id=parent_id, name=parent_name)
    channel = SimpleNamespace(id=channel_id, name=channel_name, parent=parent)
    if parent_id is not None:
        channel.parent_id = parent_id
    return SimpleNamespace(
        id="m1",
        author=author,
        guild=guild,
        channel=channel,
        content=content,
        attachments=list(attachments),
        reference=reference,
        message_snapshots=list(message_snapshots),
        mentions=[_user(m) for m in mentions],
    )


def _attachment(
    *,
    attachment_id: str,
    filename: str,
    url: str = "https://example.invalid/file.txt",
    proxy_url: str = "https://cdn.example.invalid/file.txt",
    content_type: str | None = "text/plain",
    size: int = 12,
    description: str | None = None,
    spoiler: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=attachment_id,
        filename=filename,
        url=url,
        proxy_url=proxy_url,
        content_type=content_type,
        size=size,
        description=description,
        spoiler=spoiler,
    )


async def _deliver(
    msg: SimpleNamespace,
    **cfg_kwargs: Any,
) -> tuple[str, NewMessage, list[tuple[str, str, str | None]]]:
    delivered: list[tuple[str, NewMessage]] = []
    metadata: list[tuple[str, str, str | None]] = []
    channel = DiscordChannel(
        "discord",
        DiscordConnectionConfig(bot_token_env=DISCORD_BOT_ENV, **cfg_kwargs),
        "token",
        lambda jid, new_message: delivered.append((jid, new_message)),
        lambda jid, timestamp, chat_name: metadata.append((jid, timestamp, chat_name)),
    )
    channel.bot_user_id = BOT_ID

    await channel.events.handle_message(msg)

    assert len(delivered) == 1
    jid, new_message = delivered[0]
    return jid, new_message, metadata


async def test_dm_message_keys_chat_off_sender_user_id():
    jid, msg, metadata = await _deliver(
        _message(
            author=_user("5", display_name="Alice"),
            guild_id=None,
            channel_id="dm1",
            content="hello",
        ),
        dm_policy="open",
    )

    assert jid == "discord:direct:5"
    assert msg.chat_jid == jid
    assert msg.sender == "5"
    assert msg.sender_name == "Alice"
    assert msg.content == "hello"
    assert metadata[0][0] == jid
    assert metadata[0][2] == "Alice"


async def test_guild_message_keys_chat_off_channel_id():
    jid, msg, metadata = await _deliver(
        _message(
            author=_user("5", display_name="Alice"),
            guild_id="g1",
            channel_id="c1",
            channel_name="code-improver",
            mentions=(BOT_ID,),
            content="hello",
        ),
        group_policy="open",
    )

    assert jid == "discord:channel:c1"
    assert msg.chat_jid == jid
    assert msg.metadata["discord_channel_name"] == "code-improver"
    assert metadata[0][2] == "code-improver"


async def test_thread_message_uses_thread_jid_and_parent_metadata():
    jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="t1",
            channel_name="run-123",
            parent_id="c1",
            parent_name="admin",
            content="thread update",
        ),
        group_policy="allowlist",
        chat={"g1": {"channels": {"c1": {"require_mention": False}}}},
    )

    assert jid == "discord:channel:t1"
    assert msg.metadata["discord_message_id"] == "m1"
    assert msg.metadata["discord_parent_chat_jid"] == "discord:channel:c1"
    assert msg.metadata["discord_channel_name"] == "run-123"
    assert msg.metadata["discord_parent_channel_name"] == "admin"


async def test_reply_context_is_preserved_in_message_metadata():
    replied_author = SimpleNamespace(id="42", display_name="Alice")
    replied = SimpleNamespace(id="reply-1", author=replied_author, content="Original message")
    reference = SimpleNamespace(message_id="reply-1", resolved=replied)
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            content="Following up",
            reference=reference,
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.metadata["discord_message_id"] == "m1"
    assert msg.metadata["reply_to_message_id"] == "reply-1"
    assert msg.metadata["reply_to_sender"] == "Alice"
    assert msg.metadata["reply_to_text"] == "Original message"


async def test_attachments_are_preserved_in_message_metadata():
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            content="See attached",
            attachments=(
                _attachment(
                    attachment_id="a1",
                    filename="design.txt",
                    description="Architecture sketch",
                ),
            ),
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.metadata["attachments"] == [
        {
            "id": "a1",
            "filename": "design.txt",
            "url": "https://example.invalid/file.txt",
            "proxy_url": "https://cdn.example.invalid/file.txt",
            "content_type": "text/plain",
            "size": 12,
            "description": "Architecture sketch",
            "spoiler": False,
        }
    ]


async def test_forwarded_snapshot_text_falls_back_when_message_content_missing():
    snapshot = SimpleNamespace(
        type="default",
        content="Forwarded content",
        created_at=datetime(2026, 7, 7, tzinfo=UTC),
        attachments=[
            _attachment(
                attachment_id="forward-1",
                filename="trace.json",
                url="https://example.invalid/trace.json",
                proxy_url="https://cdn.example.invalid/trace.json",
                content_type="application/json",
                size=64,
            )
        ],
    )
    _jid, msg, _metadata = await _deliver(
        _message(
            author=_user("5"),
            guild_id="g1",
            channel_id="c1",
            message_snapshots=(snapshot,),
            mentions=(BOT_ID,),
        ),
        group_policy="open",
    )

    assert msg.content == "Forwarded content"
    assert msg.metadata["forwarded_messages"] == [
        {
            "content": "Forwarded content",
            "created_at": "2026-07-07T00:00:00+00:00",
            "type": "default",
            "attachments": [
                {
                    "id": "forward-1",
                    "filename": "trace.json",
                    "url": "https://example.invalid/trace.json",
                    "proxy_url": "https://cdn.example.invalid/trace.json",
                    "content_type": "application/json",
                    "size": 64,
                    "description": None,
                    "spoiler": False,
                }
            ],
        }
    ]
