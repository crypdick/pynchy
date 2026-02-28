"""Tests for security audit logging."""

from __future__ import annotations

import json

import pytest

from pynchy.state import _init_test_database, store_message_direct
from pynchy.state.connection import _get_db
from pynchy.host.container_manager.security.audit import prune_security_audit, record_security_event


@pytest.fixture(autouse=True)
async def _setup_db():
    await _init_test_database()


@pytest.mark.asyncio
async def test_record_security_event():
    """Test recording a security event stores it in messages table."""
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="read_email",
        decision="allowed",
        corruption_tainted=True,
        secret_tainted=False,
        reason="cop (corruption taint)",
        request_id="req-123",
    )

    db = _get_db()
    cursor = await db.execute("SELECT * FROM messages WHERE sender = 'security'")
    entries = await cursor.fetchall()
    assert len(entries) == 1

    metadata = json.loads(entries[0]["metadata"])
    assert metadata["tool_name"] == "read_email"
    assert metadata["decision"] == "allowed"
    assert metadata["workspace"] == "main"
    assert metadata["corruption_tainted"] is True
    assert metadata["secret_tainted"] is False
    assert metadata["request_id"] == "req-123"


@pytest.mark.asyncio
async def test_record_security_event_strips_none():
    """Test that None values are stripped from metadata."""
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="send_email",
        decision="denied",
    )

    db = _get_db()
    cursor = await db.execute("SELECT * FROM messages WHERE sender = 'security'")
    entries = await cursor.fetchall()
    assert len(entries) == 1

    metadata = json.loads(entries[0]["metadata"])
    assert "reason" not in metadata
    assert "request_id" not in metadata
    # corruption_tainted and secret_tainted are booleans (False), not None
    assert metadata["corruption_tainted"] is False
    assert metadata["secret_tainted"] is False


@pytest.mark.asyncio
async def test_record_multiple_events():
    """Test recording multiple security events."""
    for i in range(5):
        await record_security_event(
            chat_jid="group@test",
            workspace="main",
            tool_name=f"tool_{i}",
            decision="allowed",
            request_id=f"req-{i}",
        )

    db = _get_db()
    cursor = await db.execute("SELECT * FROM messages WHERE sender = 'security'")
    entries = await cursor.fetchall()
    assert len(entries) == 5


@pytest.mark.asyncio
async def test_prune_security_audit_deletes_old_entries():
    """Test that pruning removes old security entries."""
    # Insert old security audit entry
    await store_message_direct(
        id="audit-old",
        chat_jid="group@test",
        sender="security",
        sender_name="security",
        content="{}",
        timestamp="2020-01-01T00:00:00",
        is_from_me=True,
        message_type="security_audit",
    )

    deleted = await prune_security_audit(retention_days=1)
    assert deleted == 1

    db = _get_db()
    cursor = await db.execute("SELECT * FROM messages WHERE sender = 'security'")
    entries = await cursor.fetchall()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_prune_security_audit_preserves_chat_messages():
    """Test that pruning does NOT delete regular chat messages."""
    # Insert old security audit entry
    await store_message_direct(
        id="audit-old",
        chat_jid="group@test",
        sender="security",
        sender_name="security",
        content="{}",
        timestamp="2020-01-01T00:00:00",
        is_from_me=True,
        message_type="security_audit",
    )

    # Insert old regular chat message
    await store_message_direct(
        id="chat-old",
        chat_jid="group@test",
        sender="user@s.whatsapp.net",
        sender_name="User",
        content="Hello",
        timestamp="2020-01-01T00:00:00",
        is_from_me=False,
        message_type="user",
    )

    deleted = await prune_security_audit(retention_days=1)
    assert deleted == 1  # Only the security row

    db = _get_db()
    cursor = await db.execute("SELECT * FROM messages WHERE sender = 'user@s.whatsapp.net'")
    entries = await cursor.fetchall()
    assert len(entries) == 1  # Chat message preserved


@pytest.mark.asyncio
async def test_prune_security_audit_preserves_recent():
    """Test that pruning preserves recent security entries."""
    # Insert a recent security event (will have a recent timestamp)
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="read_email",
        decision="allowed",
        request_id="recent-1",
    )

    deleted = await prune_security_audit(retention_days=1)
    assert deleted == 0  # Nothing old enough to delete

    db = _get_db()
    cursor = await db.execute("SELECT * FROM messages WHERE sender = 'security'")
    entries = await cursor.fetchall()
    assert len(entries) == 1  # Recent entry preserved
