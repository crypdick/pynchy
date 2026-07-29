"""Tests for security audit logging."""

from __future__ import annotations

import pytest

from pynchy import state
from pynchy.host.container_manager.security.audit import prune_security_audit, record_security_event
from pynchy.state import get_chat_history, store_message_direct


@pytest.fixture(autouse=True)
async def _setup_db():
    await state.init_test_database()


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
        capability_id="mail.message.read",
        action_ids=("mail.message.list", "mail.message.read"),
        rule_ids=("CRED001",),
    )

    entries = await get_chat_history("group@test")
    assert len(entries) == 1

    metadata = entries[0].metadata
    assert metadata is not None
    assert metadata["tool_name"] == "read_email"
    assert metadata["decision"] == "allowed"
    assert metadata["workspace"] == "main"
    assert metadata["corruption_tainted"] is True
    assert metadata["secret_tainted"] is False
    assert metadata["request_id"] == "req-123"
    assert metadata["guarded_action_id"] == "req-123"
    assert metadata["capability_id"] == "mail.message.read"
    assert metadata["action_ids"] == ["mail.message.list", "mail.message.read"]
    assert metadata["rule_ids"] == ["CRED001"]


@pytest.mark.asyncio
async def test_record_security_event_redacts_secret_bearing_reason():
    """Audit metadata keeps correlation but never persists raw secret values."""
    raw_secret = "".join(("ghp_", "a" * 36))
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="Bash",
        decision="denied",
        reason=f"token={raw_secret}",
        request_id="guard-1",
    )

    entry = (await get_chat_history("group@test"))[0]
    assert raw_secret not in entry.content
    assert entry.metadata is not None
    assert entry.metadata["reason"] == "[redacted sensitive data: credential]"
    assert entry.metadata["guarded_action_id"] == "guard-1"


@pytest.mark.asyncio
async def test_record_security_event_uses_scanner_when_local_redaction_misses_token():
    """Provider-token formats never reach the audit log unredacted."""
    raw_secret = "".join(  # pragma: allowlist secret
        ("xoxb-", "123456789012-123456789012-abcdefghijklmnopqrstuvwxyzABCDEF")
    )
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="Bash",
        decision="denied",
        reason=f"Rejected provider token {raw_secret}",
        request_id="guard-scanner",
    )

    entry = (await get_chat_history("group@test"))[0]
    assert raw_secret not in entry.content
    assert entry.metadata is not None
    assert entry.metadata["reason"] == "[redacted: secret-bearing reason]"


@pytest.mark.asyncio
async def test_record_security_event_redacts_pii_reason():
    """PII does not enter persisted audit content or metadata."""
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="send_email",
        decision="denied",
        reason="Blocked recipient private@example.test",
        request_id="guard-pii",
    )

    entry = (await get_chat_history("group@test"))[0]
    assert "private@example.test" not in entry.content
    assert entry.metadata is not None
    assert entry.metadata["reason"] == "[redacted sensitive data: email]"


@pytest.mark.asyncio
async def test_record_security_event_strips_none():
    """Test that None values are stripped from metadata."""
    await record_security_event(
        chat_jid="group@test",
        workspace="main",
        tool_name="send_email",
        decision="denied",
    )

    entries = await get_chat_history("group@test")
    assert len(entries) == 1

    metadata = entries[0].metadata
    assert metadata is not None
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

    entries = await get_chat_history("group@test")
    assert len(entries) == 5


@pytest.mark.asyncio
async def test_prune_security_audit_deletes_old_entries():
    """Test that pruning removes old security entries."""
    # Insert old security audit entry
    await store_message_direct(
        message_id="audit-old",
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

    entries = await get_chat_history("group@test")
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_prune_security_audit_preserves_chat_messages():
    """Test that pruning does NOT delete regular chat messages."""
    # Insert old security audit entry
    await store_message_direct(
        message_id="audit-old",
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
        message_id="chat-old",
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

    entries = await get_chat_history("group@test")
    assert [entry.sender for entry in entries] == ["user@s.whatsapp.net"]


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

    entries = await get_chat_history("group@test")
    assert len(entries) == 1  # Recent entry preserved
