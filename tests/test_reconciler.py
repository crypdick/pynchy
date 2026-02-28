"""Tests for the unified channel reconciler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from pynchy.chat.reconciler import reconcile_all_channels, reset_cooldowns
from pynchy.config_models import OwnerConfig, WorkspaceConfig, WorkspaceDefaultsConfig
from pynchy.db import (
    _init_test_database,
    get_channel_cursor,
    get_pending_outbound,
    record_outbound,
    set_channel_cursor,
)
from pynchy.db._connection import _get_db
from pynchy.types import InboundFetchResult, NewMessage, WorkspaceProfile
from tests.conftest import make_settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="group@g.us",
    name="Test",
    folder="test",
    trigger="@pynchy",
    added_at="2024-01-01",
)


def _make_channel(
    *,
    name: str = "slack",
    connected: bool = True,
    owns: bool = True,
    inbound: list[NewMessage] | None = None,
    high_water_mark: str = "",
) -> MagicMock:
    ch = MagicMock()
    ch.name = name
    ch.is_connected.return_value = connected
    ch.owns_jid = MagicMock(return_value=owns)
    ch.send_message = AsyncMock()
    msgs = inbound or []
    # Default high_water_mark to the latest message timestamp if not provided
    hwm = high_water_mark or (msgs[-1].timestamp if msgs else "")
    ch.fetch_inbound_since = AsyncMock(
        return_value=InboundFetchResult(messages=msgs, high_water_mark=hwm)
    )
    return ch


def _make_deps(
    channels: list | None = None,
    workspaces: dict | None = None,
) -> MagicMock:
    deps = MagicMock()
    deps.channels = channels or []
    deps.workspaces = workspaces or {}
    deps.queue = MagicMock()
    deps._ingest_user_message = AsyncMock()
    return deps


@pytest.fixture()
async def _db():
    await _init_test_database()
    # Seed chat rows for the FK constraint
    db = _get_db()
    for jid in ("group@g.us", "admin@g.us"):
        await db.execute(
            "INSERT INTO chats (jid, last_message_time) VALUES (?, ?)",
            (jid, "2024-01-01T00:00:00"),
        )
    await db.commit()


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    """Clear cooldowns before each test so reconciliation always runs."""
    reset_cooldowns()
    yield
    reset_cooldowns()


@pytest.fixture(autouse=True)
def _permissive_sender_defaults(monkeypatch):
    """Default to wildcard allowed_users so tests that don't care about sender
    filtering are unaffected.  Tests in TestSenderFilter override this with
    restrictive settings via monkeypatch."""
    monkeypatch.setattr(
        "pynchy.config._settings",
        make_settings(workspace_defaults=WorkspaceDefaultsConfig(allowed_users=["*"])),
    )


# ---------------------------------------------------------------------------
# Inbound reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestInboundReconciliation:
    @pytest.mark.asyncio
    async def test_ingests_new_messages(self):
        msg = NewMessage(
            id="msg-1",
            chat_jid="slack:C123",
            sender="U1",
            sender_name="Alice",
            content="hello",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")

        await reconcile_all_channels(deps)

        deps._ingest_user_message.assert_awaited_once()
        ingested_msg = deps._ingest_user_message.call_args[0][0]
        assert ingested_msg.chat_jid == "group@g.us"  # remapped to canonical
        deps.queue.enqueue_message_check.assert_called_once_with("group@g.us")

    @pytest.mark.asyncio
    async def test_advances_inbound_cursor(self):
        msg = NewMessage(
            id="msg-1",
            chat_jid="slack:C123",
            sender="U1",
            sender_name="Alice",
            content="hello",
            timestamp="2024-06-01T12:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")

        await reconcile_all_channels(deps)

        cursor = await get_channel_cursor("slack", "group@g.us", "inbound")
        assert cursor == "2024-06-01T12:00:00"

    @pytest.mark.asyncio
    async def test_skips_channel_without_jid_or_ownership(self):
        ch = _make_channel(owns=False)
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        ch.fetch_inbound_since.assert_not_awaited()


# ---------------------------------------------------------------------------
# Outbound retry
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestOutboundRetry:
    @pytest.mark.asyncio
    async def test_retries_pending_outbound(self):
        # Record a pending outbound message
        await record_outbound("group@g.us", "retry me", "broadcast", ["slack"])

        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        ch.send_message.assert_awaited_once()
        args = ch.send_message.call_args[0]
        assert args[1] == "retry me"

        # Should be marked as delivered
        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_records_error_on_retry_failure(self):
        await record_outbound("group@g.us", "fail me", "broadcast", ["slack"])

        ch = _make_channel()
        ch.send_message.side_effect = OSError("network down")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        # Still pending (error recorded, delivered_at still NULL)
        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_preserves_ordering_on_failure(self):
        """When a delivery fails, later messages are not sent (ordering preserved)."""
        await record_outbound("group@g.us", "first", "broadcast", ["slack"])
        await record_outbound("group@g.us", "second", "broadcast", ["slack"])

        ch = _make_channel()
        ch.send_message.side_effect = OSError("network down")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        # Only one send attempted (breaks after first failure)
        assert ch.send_message.await_count == 1
        # Both still pending
        pending = await get_pending_outbound("slack", "group@g.us")
        assert len(pending) == 2


# ---------------------------------------------------------------------------
# Cooldown behaviour
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestCooldown:
    @pytest.mark.asyncio
    async def test_second_call_within_cooldown_is_skipped(self):
        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)
        first_count = ch.fetch_inbound_since.await_count

        # Second call — should be skipped due to cooldown
        await reconcile_all_channels(deps)
        assert ch.fetch_inbound_since.await_count == first_count

    @pytest.mark.asyncio
    async def test_runs_after_cooldown_reset(self):
        ch = _make_channel()
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)
        reset_cooldowns()
        await reconcile_all_channels(deps)

        assert ch.fetch_inbound_since.await_count == 2


# ---------------------------------------------------------------------------
# Cursor GC
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_db")
class TestCursorGC:
    @pytest.mark.asyncio
    async def test_prunes_stale_cursors_after_reconciliation(self):
        """Cursors for channels not in deps.channels are pruned."""
        await set_channel_cursor("dead-channel", "group@g.us", "inbound", "2024-01-01")
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-06-01")

        ch = _make_channel(name="slack")
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )

        await reconcile_all_channels(deps)

        assert await get_channel_cursor("dead-channel", "group@g.us", "inbound") == ""
        assert await get_channel_cursor("slack", "group@g.us", "inbound") == "2024-06-01"


# ---------------------------------------------------------------------------
# Sender filter — reconciler must match _route_incoming_group behavior
# ---------------------------------------------------------------------------


ADMIN_GROUP = WorkspaceProfile(
    jid="admin@g.us",
    name="Admin",
    folder="admin",
    trigger="@pynchy",
    added_at="2024-01-01",
    is_admin=True,
)


def _owner_settings(*, workspace_folder: str = "test", **ws_overrides):
    """Settings with owner-only allowed_users for a workspace."""
    ws_kwargs = {"name": workspace_folder, "allowed_users": ["owner"], **ws_overrides}
    return make_settings(
        owner=OwnerConfig(slack="U04OWNER"),
        workspaces={workspace_folder: WorkspaceConfig(**ws_kwargs)},
    )


@pytest.mark.usefixtures("_db")
class TestSenderFilter:
    """Reconciler must apply the sender filter — disallowed senders are not ingested."""

    @pytest.mark.asyncio
    async def test_disallowed_sender_not_ingested(self, monkeypatch):
        """Recovered messages from disallowed senders are skipped."""
        msg = NewMessage(
            id="msg-intruder",
            chat_jid="slack:C123",
            sender="U04INTRUDER",
            sender_name="Intruder",
            content="hack the planet",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
        monkeypatch.setattr("pynchy.config._settings", _owner_settings())

        await reconcile_all_channels(deps)

        deps._ingest_user_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allowed_sender_ingested(self, monkeypatch):
        """Recovered messages from allowed senders ARE ingested."""
        msg = NewMessage(
            id="msg-owner",
            chat_jid="slack:C123",
            sender="U04OWNER",
            sender_name="Owner",
            content="hello",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"group@g.us": TEST_GROUP},
        )
        await set_channel_cursor("slack", "group@g.us", "inbound", "2024-01-01T00:00:00")
        monkeypatch.setattr("pynchy.config._settings", _owner_settings())

        await reconcile_all_channels(deps)

        deps._ingest_user_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_admin_group_bypasses_sender_filter(self, monkeypatch):
        """Admin groups accept all senders — no filtering applied."""
        msg = NewMessage(
            id="msg-random",
            chat_jid="slack:C123",
            sender="U04RANDOM",
            sender_name="Random",
            content="admin stuff",
            timestamp="2024-06-01T00:00:00",
        )
        ch = _make_channel(inbound=[msg])
        deps = _make_deps(
            channels=[ch],
            workspaces={"admin@g.us": ADMIN_GROUP},
        )
        await set_channel_cursor("slack", "admin@g.us", "inbound", "2024-01-01T00:00:00")
        # Even with restrictive owner-only settings, admin groups pass everything
        monkeypatch.setattr(
            "pynchy.config._settings",
            _owner_settings(workspace_folder="admin", is_admin=True),
        )

        await reconcile_all_channels(deps)

        deps._ingest_user_message.assert_awaited_once()
